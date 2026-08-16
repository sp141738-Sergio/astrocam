import os
import time
import threading
import subprocess
import numpy as np
import cv2
from flask import Flask, Response, send_from_directory, redirect
from gpiozero import Button, RotaryEncoder
from luma.core.interface.serial import spi
from luma.core.render import canvas
from luma.oled.device import sh1106
from PIL import ImageFont

from gpiozero.pins.lgpio import LGPIOFactory
from gpiozero import Device

Device.pin_factory = LGPIOFactory()

# --- Глобальные переменные для накопления ---
# Количество кадров для усреднения (регулируемый параметр)
frame_accumulator = None
frame_counter = 0
STACK_SIZE = 5   # можно регулировать через меню
averaged_frame = None
alpha = 0.5  # будет меняться в зависимости от stack_size

print("Starting AstroCam All-in-One...")

FOLDER = '/home/sergio'
app = Flask(__name__)

device = None
try:
    serial = spi(port=0, device=0, gpio_DC=24, gpio_RST=25)
    device = sh1106(serial)
    print("OLED SPI init success!")
except Exception as e:
    print(f"OLED Error: {e}")

try:
    font_normal = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 8)
    font_focus = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
except IOError:
    font_normal = ImageFont.load_default()
    font_focus = ImageFont.load_default()

encoder = RotaryEncoder(a=27, b=17, max_steps=14)
btn_enc = Button(22, pull_up=True)
btn_shutter = Button(5, pull_up=True)

menu_items = [
    "Preview Shutter",
    "Preview Gain",
    "PHOTO Shutter",
    "PHOTO Gain",
    "Image Rotation",
    "B&W Night Mode",
    "Brightness",
    "Contrast",
    "Saturation",
    "Exposure Comp.",
    "White Balance",
    "Denoise",
    "Stack Size",
    "Show IP"          # новый пункт
]
current_item = 0
edit_mode = False

cam_params = {
    "preview_shutter": 100000,
    "preview_gain": 12,
    "photo_shutter": 500000,
    "photo_gain": 4,
    "flip": "BOTH",
    "mono": "OFF",
    # Новые параметры
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "ev": 0.0,
    "awb": "auto",
    "denoise": "off",   # on / off
    "stack_size": 5  # Количество кадров для усреднения
}

flip_options = ["NONE", "VERT", "HORIZ", "BOTH"]
mono_options = ["OFF", "ON"]

camera_lock = threading.Lock()
stream_process = None  # Истинное имя процесса в этом скрипте

# Глобальные переменные
averaged_frame = None
alpha = 0.5  # будет меняться в зависимости от stack_size

import time
import numpy as np
import cv2

def generate_frames():
    global averaged_frame, alpha

    while True:
        raw_frame = None
        
        # 1. Быстро забираем кадр под блокировкой и сразу её отпускаем
        with camera_lock:
            # ... захват кадра в raw_frame ...
            raw_frame = frame  # Имитация вашей строки захвата

        # 2. Если кадр не захвачен, делаем паузу и пробуем снова БЕЗ блокировки
        if not raw_frame:
            time.sleep(0.04)
            continue

        final_frame = None

        # 3. Вся тяжелая обработка идет ВНЕ camera_lock
        np_frame = np.frombuffer(raw_frame, dtype=np.uint8)
        img_array = cv2.imdecode(np_frame, cv2.IMREAD_COLOR)
        
        if img_array is not None:
            # Инициализация или обновление усреднённого кадра
            if averaged_frame is None:
                averaged_frame = img_array.astype(np.float32)
            else:
                # Формула EWMA
                cv2.accumulateWeighted(img_array, averaged_frame, alpha)
            
            # Преобразуем обратно в uint8
            display_frame = cv2.convertScaleAbs(averaged_frame)
            
            # Кодируем в JPEG
            _, encoded = cv2.imencode('.jpg', display_frame)
            final_frame = encoded.tobytes()
        else:
            # Если декодирование не удалось, отдаем сырой кадр (если он jpeg)
            final_frame = raw_frame

        # 4. Отправляем готовый кадр в поток
        if final_frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + final_frame + b'\r\n')
            
        time.sleep(0.04)


@app.route('/')
def index():
    all_files = os.listdir(FOLDER)
    photos = [f for f in all_files if f.startswith('astro_') and f.lower().endswith(('.jpg', '.jpeg', '.png'))]
    photos.sort(reverse=True)

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AstroCam Remote</title>
        <style>
            body { background: #121212; color: #fff; text-align: center; font-family: Arial; margin: 0; padding: 10px; }
            h1 { font-size: 18px; color: #ff3333; margin-bottom: 5px; }
            .stream-box { margin-bottom: 20px; }
            .stream-img { width: 100%; max-width: 600px; height: auto; border: 2px solid #333; border-radius: 8px; }
            h2 { font-size: 16px; color: #00ff00; border-top: 1px solid #333; padding-top: 15px; }
            .gallery { display: flex; flex-direction: column; align-items: center; }
            .photo-card { background: #222; border: 1px solid #333; margin: 8px 0; padding: 10px; border-radius: 8px; width: 100%; max-width: 500px; }
            .photo-img { width: 100%; height: auto; border-radius: 5px; margin: 5px 0; }
            .btn { display: inline-block; padding: 8px 12px; margin: 5px; border-radius: 4px; font-weight: bold; text-decoration: none; font-size: 12px; }
            .btn-download { background: #00aa00; color: #fff; }
            .btn-delete { background: #cc0000; color: #fff; }
        </style>
    </head>
    <body>
        <h1>🔭 AstroCam: Live Preview</h1>
        <div class="stream-box">
            <img class="stream-img" src="/video_feed">
        </div>
        <h2>📸 Captured High-Res Photos</h2>
        <div class="gallery">
    """
    if not photos:
        html += "<p style='color:#666;'>No photos yet. Press the physical shutter button!</p>"
    else:
        for p in photos:
            html += f"""
            <div class="photo-card">
                <div style="font-size: 11px; color: #aaa;">{p}</div>
                <img class="photo-img" src="/view/{p}">
                <div>
                    <a class="btn btn-download" href="/view/{p}" download>💾 Download</a>
                    <a class="btn btn-delete" href="/delete/{p}" onclick="return confirm('Delete this file?');">❌ Delete</a>
                </div>
            </div>
            """
    html += "</div></body></html>"
    return html


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/view/<filename>')
def view_file(filename):
    return send_from_directory(FOLDER, filename)


@app.route('/delete/<filename>')
def delete_file(filename):
    if filename.startswith('astro_') and filename.endswith('.jpg'):
        file_path = os.path.join(FOLDER, filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    return redirect('/')


def get_val_text(item_name):
    if item_name == "Preview Shutter":
        return f"{cam_params['preview_shutter'] / 1000000:.2f}s"
    elif item_name == "Preview Gain":
        return f"x{cam_params['preview_gain']}"
    elif item_name == "PHOTO Shutter":
        return f"{cam_params['photo_shutter'] / 1000000:.2f}s"
    elif item_name == "PHOTO Gain":
        return f"x{cam_params['photo_gain']}"
    elif item_name == "Image Rotation":
        return cam_params["flip"]
    elif item_name == "B&W Night Mode":
        return cam_params["mono"]
    elif item_name == "Brightness":
        return f"{cam_params['brightness']:.1f}"
    elif item_name == "Contrast":
        return f"{cam_params['contrast']:.1f}"
    elif item_name == "Saturation":
        return f"{cam_params['saturation']:.1f}"
    elif item_name == "Exposure Comp.":
        return f"{cam_params['ev']:+.1f}EV"
    elif item_name == "White Balance":
        return cam_params["awb"]
    elif item_name == "Denoise":
        return "ON" if cam_params["denoise"] == "on" else "OFF"
    elif item_name == "Stack Size":
        return str(cam_params["stack_size"])
    elif item_name == "Show IP":
        return ""   # не используется
    return ""

def get_current_ip():
    try:
        import subprocess
        result = subprocess.check_output(["ip", "addr", "show", "wlan0"], stderr=subprocess.DEVNULL).decode()
        for line in result.split('\n'):
            if "inet " in line and "scope global" in line:
                ip = line.strip().split()[1].split('/')[0]
                return f"{ip}:5000"
        return "No IP"
    except:
        return "Error"

def draw_menu():
    if device is None:
        return
    try:
        with canvas(device) as draw:
            draw.rectangle(device.bounding_box, fill="black")
            focus_y_start, focus_y_end = 21, 43

            if not edit_mode:
                draw.rectangle((2, focus_y_start, 126, focus_y_end), outline="white", fill="black")
            else:
                draw.rectangle((2, focus_y_start, 126, focus_y_end), outline="white", fill="white")

            idx_prev = (current_item - 1) % len(menu_items)
            idx_next = (current_item + 1) % len(menu_items)

            item_p = menu_items[idx_prev]
            draw.text((8, 4), item_p, fill="gray", font=font_normal)
            draw.text((95, 4), get_val_text(item_p), fill="gray", font=font_normal)

            item_c = menu_items[current_item]
            val_c = get_val_text(item_c)
            text_color = "black" if edit_mode else "white"

            if item_c == "Show IP":
                # Вместо названия показываем IP
                ip_display = get_current_ip()
                draw.text((8, focus_y_start + 4), ip_display, fill=text_color, font=font_focus)
                # Справа ничего не выводим
            else:
               # Обычное отображение: название слева, значение справа
                draw.text((8, focus_y_start + 4), item_c, fill=text_color, font=font_focus)
                x_pos = 80 if len(val_c) > 5 else 95
                draw.text((x_pos, focus_y_start + 4), val_c, fill=text_color, font=font_focus)

                # Следующий пункт
            item_n = menu_items[idx_next]
            draw.text((8, 48), item_n, fill="gray", font=font_normal)
            draw.text((95, 48), get_val_text(item_n), fill="gray", font=font_normal)

    except:
        pass


def on_rotate():
    global current_item, edit_mode
    steps = encoder.steps
    encoder.steps = 0

    if not edit_mode:
        current_item = (current_item - steps) % len(menu_items)
    else:
        active_param = menu_items[current_item]
        if active_param == "Preview Shutter":
            cam_params["preview_shutter"] = max(10000, min(200000, cam_params["preview_shutter"] - steps * 10000))
        elif active_param == "Preview Gain":
            cam_params["preview_gain"] = max(1, min(16, cam_params["preview_gain"] - steps))
        elif active_param == "PHOTO Shutter":
            cam_params["photo_shutter"] = max(10000, min(2000000, cam_params["photo_shutter"] - steps * 50000))
        elif active_param == "PHOTO Gain":
            cam_params["photo_gain"] = max(1, min(16, cam_params["photo_gain"] - steps))
        elif active_param == "Image Rotation":
            idx = flip_options.index(cam_params["flip"])
            cam_params["flip"] = flip_options[(idx - steps) % len(flip_options)]
        elif active_param == "B&W Night Mode":
            idx = mono_options.index(cam_params["mono"])
            cam_params["mono"] = mono_options[(idx - steps) % len(mono_options)]
        elif active_param == "Brightness":
            cam_params["brightness"] = max(-1.0, min(1.0, cam_params["brightness"] - steps * 0.1))
        elif active_param == "Contrast":
            cam_params["contrast"] = max(-2.0, min(2.0, cam_params["contrast"] - steps * 0.1))
        elif active_param == "Saturation":
            cam_params["saturation"] = max(-2.0, min(2.0, cam_params["saturation"] - steps * 0.1))
        elif active_param == "Exposure Comp.":
            new_ev = cam_params["ev"] - steps * 0.5
            cam_params["ev"] = max(-2.0, min(2.0, new_ev))
        elif active_param == "White Balance":
            awb_options = ["auto", "tungsten", "fluorescent", "daylight", "cloudy", "shade"]
            idx = awb_options.index(cam_params["awb"])
            cam_params["awb"] = awb_options[(idx - steps) % len(awb_options)]
        elif active_param == "Denoise":
            cam_params["denoise"] = "off" if cam_params["denoise"] == "on" else "on"
        elif active_param == "Denoise":
            cam_params["denoise"] = "off" if cam_params["denoise"] == "on" else "on"
        elif active_param == "Stack Size":
            new_size = cam_params["stack_size"] - steps
            cam_params["stack_size"] = max(2, min(30, new_size))
            global alpha
            alpha = 1.0 / cam_params["stack_size"]
        elif active_param == "Show IP":
            # Ничего не делаем, просто прокручиваем мимо
            pass

    draw_menu()


def on_click():
    global edit_mode
    edit_mode = not edit_mode
    draw_menu()


# Исправленная и защищенная функция безопасного выключения
def safe_shutdown():
    global stream_process
    if stream_process:
        try:
            stream_process.kill()
        except:
            pass

    if device is not None:
        try:
            with canvas(device) as draw:
                draw.rectangle(device.bounding_box, outline="white", fill="black")
                draw.text((15, 20), "SHUTDOWN...", fill="white", font=font_focus)
        except:
            pass
    time.sleep(2.0)
    os.system("sudo shutdown -h now")


def snap_photo():
    def worker():
        with camera_lock:
            filename = f"/home/sergio/astro_{int(time.time())}.png"
            snap_cmd = [
                "rpicam-still", "-o", filename,
                "--shutter", str(cam_params["photo_shutter"]),
                "--gain", str(cam_params["photo_gain"]),
                "--timeout", "2000",          # ждём 2 секунды перед снимком
                "--encoding", "png"
            ]
            if cam_params["flip"] in ["VERT", "BOTH"]:
                snap_cmd.append("--vflip")
            if cam_params["flip"] in ["HORIZ", "BOTH"]:
                snap_cmd.append("--hflip")
            snap_cmd.extend(["--brightness", str(cam_params["brightness"])])
            snap_cmd.extend(["--contrast", str(cam_params["contrast"])])
            snap_cmd.extend(["--saturation", str(cam_params["saturation"])])
            snap_cmd.extend(["--denoise", cam_params["denoise"]])
            snap_cmd.extend(["--ev", str(cam_params["ev"])])
            snap_cmd.extend(["--awb", cam_params["awb"]])

            if cam_params["mono"] == "ON":
                snap_cmd.extend(["--saturation", "0.0", "--denoise", "off"])

            subprocess.run(snap_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Убрали draw_menu() отсюда, чтобы не провоцировать конфликт потоков GPIO

    threading.Thread(target=worker).start()


# Настройка обработчиков
encoder.when_rotated = on_rotate
btn_enc.when_pressed = on_click
btn_enc.hold_time = 3.0
btn_enc.when_held = safe_shutdown
btn_shutter.when_pressed = snap_photo

draw_menu()


def run_flask():
    app.run(host='0.0.0.0', port=5000, threaded=True, debug=True)


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nAstroCam stopped.")
