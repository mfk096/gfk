import base64
import io
import json
import os
import random
import re
import string
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

import requests
from PIL import Image
from pylibdmtx.pylibdmtx import encode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
import tkinter as tk
from tkinter import ttk, messagebox


# --------------------------
# Настройки интеграции ЧЗ
# --------------------------
# Для production замените значениями вашей организации.
CZ_API_BASE = os.getenv("CZ_API_BASE", "https://ismp.crpt.ru/api/v3/lk")
CZ_TOKEN = os.getenv("CZ_TOKEN", "")
CZ_OMS_ID = os.getenv("CZ_OMS_ID", "")
CZ_PRODUCT_GROUP = os.getenv("CZ_PRODUCT_GROUP", "food")
CZ_CREATE_CODES_ENDPOINT = os.getenv("CZ_CREATE_CODES_ENDPOINT", "/codes/orders")
CZ_ORDER_STATUS_ENDPOINT = os.getenv("CZ_ORDER_STATUS_ENDPOINT", "/codes/orders/{order_id}")
CZ_SIGNATURE_MODE = os.getenv("CZ_SIGNATURE_MODE", "none")  # none|base64|cryptopro
CZ_CRYPTCP_PATH = os.getenv("CZ_CRYPTCP_PATH", "cryptcp")


GS = chr(29)


@dataclass
class AppConfig:
    api_base: str
    token: str
    oms_id: str
    product_group: str
    create_endpoint: str
    status_endpoint: str
    signature_mode: str
    cryptcp_path: str


def build_config() -> AppConfig:
    return AppConfig(
        api_base=CZ_API_BASE.rstrip("/"),
        token=CZ_TOKEN.strip(),
        oms_id=CZ_OMS_ID.strip(),
        product_group=CZ_PRODUCT_GROUP.strip() or "food",
        create_endpoint=CZ_CREATE_CODES_ENDPOINT,
        status_endpoint=CZ_ORDER_STATUS_ENDPOINT,
        signature_mode=CZ_SIGNATURE_MODE.strip().lower(),
        cryptcp_path=CZ_CRYPTCP_PATH,
    )


def random_ascii(length: int) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


def has_crypto_tail(dm_code: str) -> bool:
    # Проверяем, что присутствуют AI 91 и AI 92 с непустыми данными.
    # Для ЧЗ обычно 91 и 92 идут после GS-разделителя, но допустим и без него.
    normalized = dm_code.replace("\\u001d", GS).replace("\x1d", GS)
    return bool(re.search(r"91[^\x1d]{2,}.*92[^\x1d]{4,}", normalized))


def build_demo_codes(gtin: str, qty: int) -> List[str]:
    result = []
    for _ in range(qty):
        serial = random_ascii(13)
        tail_91 = random_ascii(4)
        tail_92 = base64.urlsafe_b64encode(os.urandom(12)).decode("ascii").rstrip("=")
        dm = f"01{gtin}21{serial}{GS}91{tail_91}{GS}92{tail_92}"
        result.append(dm)
    return result


class ChestnyZnakClient:
    """Клиент, скрывающий технические этапы от GUI."""

    def __init__(self, config: AppConfig, timeout: int = 30):
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def _sign_payload(self, payload: dict) -> Tuple[dict, str]:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        mode = self.config.signature_mode

        if mode == "none":
            return payload, "без подписи (режим разработки)"

        if mode == "base64":
            envelope = {
                "document": payload,
                "signature": base64.b64encode(payload_json.encode("utf-8")).decode("ascii"),
            }
            return envelope, "base64-подпись (заглушка)"

        if mode == "cryptopro":
            signature = self._sign_with_cryptopro(payload_json)
            envelope = {"document": payload, "signature": signature}
            return envelope, "CryptoPro"

        raise ValueError(f"Неизвестный режим подписи: {mode}")

    def _sign_with_cryptopro(self, payload_json: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "payload.json"
            sig = Path(tmp) / "payload.sig"
            src.write_text(payload_json, encoding="utf-8")
            cmd = [
                self.config.cryptcp_path,
                "-sign",
                "-detached",
                "-base64",
                str(src),
                str(sig),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"CryptoPro ошибка: {proc.stderr.strip() or proc.stdout.strip()}")
            return sig.read_text(encoding="utf-8").strip()

    def create_and_wait_codes(self, gtin: str, qty: int, status_cb=None) -> Tuple[List[str], str]:
        if not self.config.token or not self.config.oms_id:
            if status_cb:
                status_cb("Нет боевых реквизитов, включён демонстрационный режим.")
            return build_demo_codes(gtin, qty), "demo"

        payload = {
            "productGroup": self.config.product_group,
            "omsId": self.config.oms_id,
            "gtin": gtin,
            "quantity": qty,
        }
        signed_payload, sign_mode = self._sign_payload(payload)
        if status_cb:
            status_cb(f"Заявка подписана: {sign_mode}. Отправка в ЧЗ…")

        create_url = f"{self.config.api_base}{self.config.create_endpoint}"
        resp = requests.post(
            create_url,
            headers=self._headers(),
            data=json.dumps(signed_payload, ensure_ascii=False),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        create_data = resp.json()
        order_id = create_data.get("orderId") or create_data.get("id")
        if not order_id:
            raise RuntimeError(f"Не получен orderId: {create_data}")

        if status_cb:
            status_cb(f"Заказ {order_id} создан. Ожидание кодов…")

        status_url = f"{self.config.api_base}{self.config.status_endpoint.format(order_id=order_id)}"
        deadline = time.time() + 180
        while time.time() < deadline:
            st = requests.get(status_url, headers=self._headers(), timeout=self.timeout)
            st.raise_for_status()
            data = st.json()
            raw_codes = data.get("codes") or data.get("cis") or []
            if raw_codes:
                codes = [c.get("code") if isinstance(c, dict) else c for c in raw_codes]
                return [c for c in codes if c], str(order_id)
            time.sleep(2)

        raise TimeoutError("Честный ЗНАК не вернул коды в течение 180 секунд")


def dm_image_from_code(dm_code: str, scale: int = 6) -> Image.Image:
    data = dm_code.encode("utf-8")
    encoded = encode(data)
    img = Image.frombytes("RGB", (encoded.width, encoded.height), encoded.pixels)
    return img.resize((encoded.width * scale, encoded.height * scale), Image.NEAREST)


def export_pdf_with_datamatrix(codes: List[str], output_path: Path) -> None:
    c = canvas.Canvas(str(output_path), pagesize=A4)
    page_w, page_h = A4

    margin = 10 * mm
    cols = 3
    rows = 7
    cell_w = (page_w - margin * 2) / cols
    cell_h = (page_h - margin * 2) / rows

    x = y = 0
    for idx, code in enumerate(codes, 1):
        img = dm_image_from_code(code)
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        img_reader = ImageReader(bio)

        col = x % cols
        row = y % rows
        px = margin + col * cell_w
        py = page_h - margin - (row + 1) * cell_h

        side = min(cell_w, cell_h) * 0.65
        c.drawImage(
            image=img_reader,
            x=px + (cell_w - side) / 2,
            y=py + (cell_h - side) / 2 + 5,
            width=side,
            height=side,
            preserveAspectRatio=True,
            mask="auto",
        )

        short_text = code.replace(GS, "␝")[:56]
        c.setFont("Helvetica", 6)
        c.drawCentredString(px + cell_w / 2, py + 3, short_text)

        x += 1
        if x % cols == 0:
            y += 1
        if y and y % rows == 0 and idx < len(codes):
            c.showPage()
            x = 0
            y = 0

    c.save()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ЧЗ DataMatrix → PDF")
        self.root.geometry("520x240")
        self.config = build_config()
        self.client = ChestnyZnakClient(self.config)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="GTIN (14 цифр):").grid(row=0, column=0, sticky="w")
        self.gtin_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.gtin_var, width=30).grid(row=0, column=1, sticky="we", pady=4)

        ttk.Label(frame, text="Количество:").grid(row=1, column=0, sticky="w")
        self.qty_var = tk.StringVar(value="10")
        ttk.Entry(frame, textvariable=self.qty_var, width=30).grid(row=1, column=1, sticky="we", pady=4)

        self.btn = ttk.Button(frame, text="Сделать PDF", command=self.on_generate)
        self.btn.grid(row=2, column=0, columnspan=2, sticky="we", pady=(12, 8))

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(frame, textvariable=self.status_var, foreground="#1f4a7a", wraplength=470).grid(
            row=3, column=0, columnspan=2, sticky="w"
        )

        frame.columnconfigure(1, weight=1)

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def on_generate(self):
        gtin = self.gtin_var.get().strip()
        qty_s = self.qty_var.get().strip()

        if not re.fullmatch(r"\d{14}", gtin):
            messagebox.showerror("Ошибка", "GTIN должен содержать ровно 14 цифр.")
            return

        try:
            qty = int(qty_s)
            if qty <= 0 or qty > 5000:
                raise ValueError
        except ValueError:
            messagebox.showerror("Ошибка", "Количество должно быть числом от 1 до 5000.")
            return

        self.btn.config(state="disabled")
        worker = threading.Thread(target=self.generate_pdf_job, args=(gtin, qty), daemon=True)
        worker.start()

    def generate_pdf_job(self, gtin: str, qty: int):
        try:
            self.root.after(0, lambda: self.set_status("Формирование заказа кодов…"))
            codes, source = self.client.create_and_wait_codes(gtin, qty, status_cb=lambda m: self.root.after(0, lambda: self.set_status(m)))

            self.root.after(0, lambda: self.set_status("Проверка криптохвостов 91/92…"))
            valid = [c for c in codes if has_crypto_tail(c)]
            invalid = [c for c in codes if not has_crypto_tail(c)]

            if not valid:
                raise RuntimeError("Нет валидных кодов с криптохвостами 91/92.")

            out_name = f"datamatrix_{gtin}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            out_path = Path.cwd() / out_name

            self.root.after(0, lambda: self.set_status("Сборка PDF для печати…"))
            export_pdf_with_datamatrix(valid, out_path)

            msg = f"Готово: {out_path.name}. Валидных кодов: {len(valid)}"
            if invalid:
                msg += f". Некорректных: {len(invalid)}"
            if source == "demo":
                msg += " (демо-режим: задайте CZ_TOKEN и CZ_OMS_ID для боевого API)"

            self.root.after(0, lambda: self.set_status(msg))
            if invalid:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning(
                        "Проверка кодов",
                        f"Некоторые коды не содержат корректные 91/92: {len(invalid)} шт.\n"
                        f"В PDF добавлены только валидные коды ({len(valid)} шт.).",
                    ),
                )
            else:
                self.root.after(0, lambda: messagebox.showinfo("Успех", f"PDF создан:\n{out_path}"))

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", str(e)))
            self.root.after(0, lambda: self.set_status(f"Ошибка: {e}"))
        finally:
            self.root.after(0, lambda: self.btn.config(state="normal"))


def main():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
