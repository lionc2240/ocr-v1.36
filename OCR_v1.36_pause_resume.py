import httplib2
import os
import io
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import configparser
from apiclient import discovery
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage
from apiclient.http import MediaFileUpload, MediaIoBaseDownload
from pathlib import Path
import shutil
import time
import tkinter as tk
from tkinter import messagebox, ttk, filedialog, scrolledtext
import concurrent.futures
import datetime
import subprocess
import re
import cv2
from PIL import Image , ImageTk
import psutil
import signal
import sys

# Cấu hình (Hằng số nên viết hoa toàn bộ)
SCOPES = "https://www.googleapis.com/auth/drive"
CLIENT_SECRET_FILE = "credentials.json"
APPLICATION_NAME = "Drive API Python Quickstart"

# Khởi tạo mặc định để tránh lỗi nếu không load được từ config
DEFAULT_FOLDER_ID = ""
DEFAULT_VIDEOSUBFINDER_PATH = r"D:\VideoSubFinder_6.10_x64\Release_x64\VideoSubFinderWXW_intel.exe"

# Biến toàn cục
FOLDER_ID = DEFAULT_FOLDER_ID
SRT_FILE_LIST = {}  # Sử dụng tên viết hoa phù hợp cho hằng số
PROGRESS_LOCK = threading.Lock()
STOP_FLAG = False
PAUSE_FLAG = threading.Event()
PAUSE_FLAG.set()  # Mặc định là không tạm dừng
LOG_FILE_PATH = None
EXTRACTED_TIME = None
DURATION = None
EVENT_HANDLER = None
OBSERVER = None
WORKER_THREADS = []


class RGBImagesEventHandler(FileSystemEventHandler):
    """Giám sát thư mục RGBImages và ghi log khi có file mới."""

    def __init__(self, video_duration="00:00:00"):
        super().__init__()
        self.video_duration = video_duration
        self.file_count = 0
        try:
            h, m, s = map(int, video_duration.split(':'))
            self.video_duration_timedelta = datetime.timedelta(hours=h, minutes=m, seconds=s)
        except ValueError:
            print("Lỗi định dạng thời lượng video, sử dụng giá trị mặc định '00:00:00'")
            self.video_duration_timedelta = datetime.timedelta()

    def on_created(self, event):
        if event.is_directory:
            return  # Bỏ qua thư mục

        file_name = os.path.basename(event.src_path)
        self.file_count += 1  # Tăng số lượng ảnh

        log_message(f"📂 RGBImages: {file_name}")

        match = re.search(r"(\d+)_(\d+)_(\d+)_(\d+)", file_name)
        if match:
            global EXTRACTED_TIME
            EXTRACTED_TIME = f"{match.group(1)}:{match.group(2)}:{match.group(3)}:{match.group(4)}"
            try:
                extracted_time_obj = datetime.datetime.strptime(EXTRACTED_TIME, "%H:%M:%S:%f").time()
                extracted_timedelta = datetime.timedelta(
                    hours=extracted_time_obj.hour,
                    minutes=extracted_time_obj.minute,
                    seconds=extracted_time_obj.second,
                    microseconds=extracted_time_obj.microsecond,
                )

                time_remaining = self.video_duration_timedelta - extracted_timedelta
                time_remaining_str = str(time_remaining)
                time_remaining_str = time_remaining_str[:-7]  # Loại bỏ microseconds

                percentage_complete = (extracted_timedelta / self.video_duration_timedelta) * 100
                percentage_complete = max(0, min(percentage_complete, 100))

                status_label.config(text=f"VSF đang chạy...👀 Còn lại: {time_remaining_str} |⏱ Tổng thời gian: {self.video_duration} |📂 Ảnh: {self.file_count}")
                root.after(0, progress_bar.config, {"value": percentage_complete})
            except ValueError as e:
                log_message(f"Lỗi xử lý thời gian: {e}")


def start_monitoring_rgbimages(rgb_images_folder, video_duration="00:00:00"):
    """Bắt đầu giám sát thư mục RGBImages."""
    if not os.path.exists(rgb_images_folder):
        log_message("❌ Thư mục RGBImages chưa được tạo, không thể giám sát.")
        return

    global OBSERVER, WORKER_THREADS, EVENT_HANDLER

    # Kiểm tra observer đang chạy chưa. Nếu rồi thì không làm gì cả.
    if OBSERVER and OBSERVER.is_alive():
        return

    OBSERVER = Observer()
    EVENT_HANDLER = RGBImagesEventHandler(video_duration)
    OBSERVER.schedule(EVENT_HANDLER, rgb_images_folder, recursive=True)
    observer_thread = threading.Thread(target=OBSERVER.start, daemon=True)
    WORKER_THREADS.append(observer_thread)
    observer_thread.start()


def wait_for_rgbimages_and_monitor(path, video_duration, retries=10, delay=1):
    """Chờ thư mục RGBImages tồn tại trước khi giám sát."""
    while retries > 0:
        if os.path.exists(path):
            log_message(f"👀 Đã thấy thư mục: {path}.\n🚀 Bắt đầu giám sát!")
            start_monitoring_rgbimages(path, video_duration)
            return
        log_message(f"⚠️ Không thấy RGBImages. Đang đợi... {retries}s còn lại")
        time.sleep(delay)
        retries -= 1

    log_message("❌ LỖI: Không thể tìm thấy thư mục RGBImages sau thời gian chờ.")


def stop_processing():
    global STOP_FLAG, PAUSE_FLAG
    STOP_FLAG = True
    PAUSE_FLAG.set()  # Đảm bảo các luồng đang tạm dừng có thể nhận tín hiệu dừng để thoát
    start_button.config(state=tk.NORMAL)
    stop_button.config(state=tk.DISABLED)
    pause_button.config(state=tk.DISABLED, text="⏸ Tạm dừng")
    VSF_button.config(state=tk.NORMAL)
    log_message("Quá trình đã được dừng.")


def toggle_pause():
    """Chuyển đổi trạng thái tạm dừng/tiếp tục."""
    global PAUSE_FLAG, pause_button
    if PAUSE_FLAG.is_set():
        PAUSE_FLAG.clear()
        pause_button.config(text="▶ Tiếp tục")
        log_message("⏸ Đã tạm dừng quá trình OCR.")
        status_label.config(text="⏸ Đang tạm dừng...")
    else:
        PAUSE_FLAG.set()
        pause_button.config(text="⏸ Tạm dừng")
        log_message("▶ Đang tiếp tục quá trình OCR.")
        status_label.config(text="▶ Đang chạy...")

try:
    import argparse

    flags = argparse.ArgumentParser(parents=[tools.argparser]).parse_args()
except ImportError:
    flags = None
    
def get_credentials():
    """Lấy chứng chỉ người dùng hợp lệ từ lưu trữ."""
    credential_path = os.path.join("./", "token.json")
    store = Storage(credential_path)
    credentials = store.get()
    if not credentials or credentials.invalid:
        flow = client.flow_from_clientsecrets(CLIENT_SECRET_FILE, SCOPES)
        flow.user_agent = APPLICATION_NAME
        if flags:
            credentials = tools.run_flow(flow, store, flags)
        else:
            credentials = tools.run(flow, store)
        print("Storing credentials to " + credential_path)
    return credentials


def save_config(
    folder_id, delete_raw_texts, delete_texts, nen_raw_texts, videosubfinder_path, crop_profiles
):
    """Lưu cấu hình vào file config.ini mà không ghi đè toàn bộ nội dung."""
    config = configparser.ConfigParser()

    # Thêm section [settings]
    config["settings"] = {
        "folder_id": folder_id,
        "delete_raw_texts": str(delete_raw_texts),
        "delete_texts": str(delete_texts),
        "nen_raw_texts": str(nen_raw_texts),
        "videosubfinder_path": videosubfinder_path,
        "threads": "20"  # Giá trị mặc định cho threads
    }

    # Thêm section [crop_profiles]
    config["crop_profiles"] = {}
    for profile, values in crop_profiles.items():
        profile_key = profile.replace(", ", "_").lower()  # Chuẩn hóa key
        config["crop_profiles"][f"{profile_key}_top"] = str(values["top"])
        config["crop_profiles"][f"{profile_key}_bottom"] = str(values["bottom"])
        config["crop_profiles"][f"{profile_key}_left"] = str(values["left"])
        config["crop_profiles"][f"{profile_key}_right"] = str(values["right"])

    # Ghi file config.ini
    with open("config.ini", "w") as configfile:
        config.write(configfile)

def load_config():
    """Đọc cấu hình từ file config.ini, tạo file mới nếu không tồn tại."""
    config = configparser.ConfigParser()
    config_file = "config.ini"

    # Các giá trị mặc định
    DEFAULT_FOLDER_ID = ""
    DEFAULT_VIDEOSUBFINDER_PATH = r"D:\VideoSubFinder_6.10\VideoSubFinderWXW_intel.exe"
    DEFAULT_THREADS = 20
    DEFAULT_DELETE_RAW_TEXTS = False
    DEFAULT_DELETE_TEXTS = False
    DEFAULT_NEN_RAW_TEXTS = False
    
    # Mặc định cho crop profiles
    default_crop_profiles = {
        "vlxx, javhd": {"top": 0.1692, "bottom": 0.0058, "left": 0, "right": 1},
        "sextop": {"top": 0.3052, "bottom": 0.0545, "left": 0, "right": 1},
        "phimKK": {"top": 0.1861, "bottom": 0.0198, "left": 0, "right": 1},
        "titdam": {"top": 0.2455, "bottom": 0.0746, "left": 0.1743, "right": 0.8322},
    }

    # Kiểm tra sự tồn tại của file config.ini
    if not os.path.exists(config_file):
        # Nếu không tồn tại, tạo file mới với giá trị mặc định
        save_config(
            DEFAULT_FOLDER_ID,
            DEFAULT_DELETE_RAW_TEXTS,
            DEFAULT_DELETE_TEXTS,
            DEFAULT_NEN_RAW_TEXTS,
            DEFAULT_VIDEOSUBFINDER_PATH,
            default_crop_profiles
        )
        # Sau khi tạo, đọc lại file để đảm bảo dữ liệu được load
        config.read(config_file)
    else:
        # Nếu file đã tồn tại, đọc bình thường
        config.read(config_file)

    # Lấy các giá trị cơ bản từ [settings]
    if "settings" in config:
        folder_id = config["settings"].get("folder_id", DEFAULT_FOLDER_ID)
        delete_raw_texts = config.getboolean("settings", "delete_raw_texts", fallback=DEFAULT_DELETE_RAW_TEXTS)
        delete_texts = config.getboolean("settings", "delete_texts", fallback=DEFAULT_DELETE_TEXTS)
        nen_raw_texts = config.getboolean("settings", "nen_raw_texts", fallback=DEFAULT_NEN_RAW_TEXTS)
        videosubfinder_path = config["settings"].get("videosubfinder_path", DEFAULT_VIDEOSUBFINDER_PATH)
        threads = config["settings"].getint("threads", DEFAULT_THREADS)
        if threads <= 0:
            threads = DEFAULT_THREADS  # Đảm bảo threads hợp lệ
    else:
        folder_id = DEFAULT_FOLDER_ID
        delete_raw_texts = DEFAULT_DELETE_RAW_TEXTS
        delete_texts = DEFAULT_DELETE_TEXTS
        nen_raw_texts = DEFAULT_NEN_RAW_TEXTS
        videosubfinder_path = DEFAULT_VIDEOSUBFINDER_PATH
        threads = DEFAULT_THREADS

    # Lấy các giá trị crop từ [crop_profiles]
    crop_profiles = default_crop_profiles.copy()  # Sao chép mặc định để chỉnh sửa nếu cần
    if "crop_profiles" in config:
        for profile in default_crop_profiles:
            profile_key = profile.replace(", ", "_").lower()  # Chuẩn hóa key (ví dụ: "vlxx, javhd" -> "vlxx_javhd")
            crop_profiles[profile]["top"] = config["crop_profiles"].getfloat(
                f"{profile_key}_top", default_crop_profiles[profile]["top"]
            )
            crop_profiles[profile]["bottom"] = config["crop_profiles"].getfloat(
                f"{profile_key}_bottom", default_crop_profiles[profile]["bottom"]
            )
            crop_profiles[profile]["left"] = config["crop_profiles"].getfloat(
                f"{profile_key}_left", default_crop_profiles[profile]["left"]
            )
            crop_profiles[profile]["right"] = config["crop_profiles"].getfloat(
                f"{profile_key}_right", default_crop_profiles[profile]["right"]
            )
            
    return folder_id, delete_raw_texts, delete_texts, nen_raw_texts, videosubfinder_path, threads, crop_profiles


def ocr_image(image, line, credentials, current_directory, progress_callback):
    """Thực hiện OCR trên một ảnh."""
    global STOP_FLAG, PAUSE_FLAG
    tries = 0
    while True:
        if STOP_FLAG:
            print("Đã dừng quá trình.")
            log_message("❌ Quá trình đã được dừng.")
            return

        # Đợi nếu đang ở trạng thái tạm dừng
        PAUSE_FLAG.wait()

        try:
            http = credentials.authorize(httplib2.Http())
            service = discovery.build("drive", "v3", http=http)
            imgfile = str(image.absolute())
            imgname = str(image.name)
            raw_txtfile = f"{current_directory}/raw_texts/{imgname[:-5]}.txt"
            txtfile = f"{current_directory}/texts/{imgname[:-5]}.txt"

            mime = "application/vnd.google-apps.document"

            res = (
                service.files()
                .create(
                    body={"name": imgname, "mimeType": mime, "parents": [FOLDER_ID]},
                    media_body=MediaFileUpload(imgfile, mimetype=mime, resumable=True),
                )
                .execute()
            )

            print(f"{imgname} Done.")
            

            downloader = MediaIoBaseDownload(
                io.FileIO(raw_txtfile, "wb"),
                service.files().export_media(fileId=res["id"], mimeType="text/plain"),
            )
            done = False
            while done is False:
                status, done = downloader.next_chunk()

            service.files().delete(fileId=res["id"]).execute()

            # Xử lý nội dung raw text
            with open(raw_txtfile, "r", encoding="utf-8") as raw_text_file:
                text_content = raw_text_file.read()

            text_content = text_content.split("\n")
            text_content = "".join(text_content[2:])
            
            log_message(f"✅ Đã OCR: {text_content[:55]}..." if len(text_content) > 55 else f"✅ Đã OCR: {text_content}")

            with open(txtfile, "w", encoding="utf-8") as text_file:
                text_file.write(text_content)

            try:
                start_hour = imgname.split("_")[0][:2]
                start_min = imgname.split("_")[1][:2]
                start_sec = imgname.split("_")[2][:2]
                start_micro = imgname.split("_")[3][:3]

                end_hour = imgname.split("__")[1].split("_")[0][:2]
                end_min = imgname.split("__")[1].split("_")[1][:2]
                end_sec = imgname.split("__")[1].split("_")[2][:2]
                end_micro = imgname.split("__")[1].split("_")[3][:3]

            except IndexError:
                print(
                    f"Error processing {imgname}: Filename format is incorrect. Please ensure the correct format is used."
                )
                return

            start_time = f"{start_hour}:{start_min}:{start_sec},{start_micro}"
            end_time = f"{end_hour}:{end_min}:{end_sec},{end_micro}"
            SRT_FILE_LIST[line] = [
                f"{line}\n",
                f"{start_time} --> {end_time}\n",
                f"{text_content}\n\n",
                "",
            ]

            progress_callback()
            break
        except Exception as e:
            tries += 1
            if tries > 5:
                print(f"Lỗi sau 5 lần thử: {e}")
                raise  # Re-raise exception sau nhiều lần thử
            time.sleep(1) # Thêm delay trước khi thử lại
            continue

def preview_srt(srt_content, save_callback):
    """Hiển thị cửa sổ xem trước nội dung SRT và cho phép lưu."""
    # Kiểm tra và khôi phục cửa sổ chính nếu nó đang bị thu nhỏ
    if root.state() == "iconic":  # "iconic" là trạng thái thu nhỏ trong Tkinter
        root.deiconify()  # Khôi phục cửa sổ chính từ thanh tác vụ

    # Tạo cửa sổ xem trước
    preview_window = tk.Toplevel(root)
    preview_window.title("Xem trước phụ đề SRT")
    preview_window.geometry("600x400")
    
    # Biến cửa sổ thành modal
    preview_window.transient(root)  # Gắn cửa sổ con vào cửa sổ cha
    preview_window.grab_set()       # Chặn tương tác với cửa sổ cha
    
    # Text widget để hiển thị nội dung SRT
    srt_text = scrolledtext.ScrolledText(preview_window, wrap="word", height=20, width=70)
    srt_text.pack(padx=5, pady=5, fill="both", expand=True)
    srt_text.insert(tk.END, srt_content)
    srt_text.config(state="normal")  # Cho phép chỉnh sửa (nếu muốn)

    # Frame chứa các nút
    button_frame = tk.Frame(preview_window)
    button_frame.pack(pady=10)

    # Nút Lưu
    save_button = tk.Button(
        button_frame,
        text="Cập nhật và Đóng",
        command=lambda: [save_callback(srt_text.get("1.0", tk.END)), preview_window.destroy()],
    )
    save_button.pack(side=tk.LEFT, padx=5)

    # Nút Hủy
    cancel_button = tk.Button(
        button_frame,
        text="Hủy",
        command=lambda: [save_callback(srt_content), preview_window.destroy()],
    )
    cancel_button.pack(side=tk.LEFT, padx=5)

    # Đảm bảo cửa sổ được căn giữa cửa sổ cha
    preview_window.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() - preview_window.winfo_width()) // 2
    y = root.winfo_y() + (root.winfo_height() - preview_window.winfo_height()) // 2
    preview_window.geometry(f"+{x}+{y}")

    # Giữ cửa sổ trên cùng (tuỳ chọn)
    preview_window.attributes("-topmost", True)

    # Đưa focus vào cửa sổ xem trước
    preview_window.focus_set()
    
    
def start_processing(
    file_sub,
    images_dirr,
    delete_raw_texts,
    delete_texts,
    nen_raw_texts,
    progress_callback,
):
    """Hàm chính xử lý và chạy OCR."""
    global total_images, completed_scans, STOP_FLAG, start_time_processing, SRT_FILE_LIST
    
    # Xóa sạch dữ liệu cũ và reset trạng thái trước khi bắt đầu phiên mới
    with PROGRESS_LOCK:
        SRT_FILE_LIST.clear()
        completed_scans = 0
    
    _, _, _, _, _, threads, _ = load_config()
    
    start_time_processing = time.time()
    completed_scans = 0
    total_images = 0

    credentials = get_credentials()
    http = credentials.authorize(httplib2.Http())
    service = discovery.build("drive", "v3", http=http)

    current_directory = Path(Path.cwd())
    images_dir = Path(images_dirr)
    raw_texts_dir = Path(f"{current_directory}/raw_texts")
    texts_dir = Path(f"{current_directory}/texts")

    subtitle_path = Path(file_sub)
    if subtitle_path.suffix != ".srt":
        subtitle_path = subtitle_path.with_suffix(".srt")

    try:
        # Kiểm tra sự tồn tại của thư mục hình ảnh
        if not images_dir.exists():
            print(f"Không tìm thấy thư mục: {images_dir}")
            log_message(f"❌ Lỗi: Thư mục {images_dir} không tồn tại.")
            messagebox.showerror(
                "Lỗi",
                f"Thư mục hình ảnh '{images_dirr}' không tồn tại.\nVui lòng kiểm tra lại đường dẫn.",
            )
            return

        if not raw_texts_dir.exists():
            raw_texts_dir.mkdir()
        if not texts_dir.exists():
            texts_dir.mkdir()

        images = []
        for extension in ("*.jpeg", "*.jpg", "*.png", "*.bmp", "*.gif"):
            images.extend(list(Path(images_dirr).rglob(extension)))

        total_images = len(images)
        
        log_message(f"|| Số luồng xử lý cùng lúc: {threads}")
        log_message(f"👀 Tổng số ảnh tìm thấy trong thư mục '{images_dirr}': {total_images}")

        if total_images == 0:
            messagebox.showerror(
                "Lỗi",
                f"Thư mục '{images_dirr}' không chứa hình ảnh hợp lệ.\n"
                "Hãy kiểm tra định dạng: JPEG, PNG, BMP, GIF.",
            )
            log_message(f"❌ Lỗi: Thư mục '{images_dirr}' không chứa hình ảnh hợp lệ.")
            return

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=threads)
        future_to_image = {
            executor.submit(
                ocr_image,
                image,
                index + 1,
                credentials,
                current_directory,
                progress_callback,
            ): image
            for index, image in enumerate(images)
        }

        try:
            for future in concurrent.futures.as_completed(future_to_image):
                if STOP_FLAG:
                    # Tắt executor mà không đợi các luồng đang chạy hoàn thành
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    future.result()
                except Exception as exc:
                    print(f"{future_to_image[future]} generated an exception: {exc}")
                else:
                    with PROGRESS_LOCK:
                        completed_scans += 1
                        progress_callback()
        finally:
            # Đảm bảo executor được đóng nếu hoàn thành bình thường
            if not STOP_FLAG:
                executor.shutdown(wait=True)

        # Tạo nội dung SRT từ SRT_FILE_LIST (kể cả khi bị dừng giữa chừng)
        srt_content = ""
        for i in sorted(SRT_FILE_LIST):
            srt_content += "".join(SRT_FILE_LIST[i])

        if STOP_FLAG:
            log_message(f"✅ Quá trình đã được dừng. Đã hoàn thành {len(SRT_FILE_LIST)}/{total_images} ảnh.")
            if not SRT_FILE_LIST:
                messagebox.showinfo("Dừng", "Quá trình đã dừng lại. Không có dữ liệu để xem trước.")
                return

        # Hàm callback để lưu file SRT
        def save_srt_content(content):
            try:
                with open(subtitle_path, "w", encoding="utf-8") as srt_file:
                    srt_file.write(content)
                log_message(f"✅ Đã lưu file SRT: {subtitle_path}")
            except Exception as e:
                log_message(f"❌ Lỗi khi lưu file SRT: {e}")
                messagebox.showerror("Lỗi", f"Không thể lưu file SRT: {e}")
            finally:
                finalize_processing(
                    subtitle_path, delete_raw_texts, delete_texts, nen_raw_texts, raw_texts_dir, texts_dir
                )

        # Hiển thị cửa sổ xem trước
        if srt_content:
            preview_srt(srt_content, save_srt_content)
        else:
            finalize_processing(
                subtitle_path, delete_raw_texts, delete_texts, nen_raw_texts, raw_texts_dir, texts_dir
            )

    except Exception as e:
        log_message(f"❌ Lỗi trong quá trình xử lý: {e}")
        messagebox.showerror("Lỗi", f"Xảy ra lỗi trong quá trình xử lý: {e}")
        finalize_processing(
            subtitle_path, delete_raw_texts, delete_texts, nen_raw_texts, raw_texts_dir, texts_dir
        )

def finalize_processing(subtitle_path, delete_raw_texts, delete_texts, nen_raw_texts, raw_texts_dir, texts_dir):
    """Xử lý các bước cuối sau khi hoàn thành hoặc lỗi."""
    if nen_raw_texts:
        try:
            zip_file_path = shutil.make_archive(
                str(subtitle_path), "zip", str(raw_texts_dir)
            )
            new_zip_file_path = zip_file_path.replace(".srt.zip", ".zip")
            os.rename(zip_file_path, new_zip_file_path)
            print(f"✅ Đã nén thư mục {raw_texts_dir}")
            log_message(f"✅ Đã nén thư mục: {raw_texts_dir}")
        except Exception as e:
            print(f"❌ Lỗi khi nén thư mục {raw_texts_dir}: {e}")
            messagebox.showerror("Lỗi", f"Không thể nén thư mục {raw_texts_dir}: {e}")

    if delete_raw_texts:
        try:
            shutil.rmtree(raw_texts_dir)
            print(f"Đã xóa thư mục {raw_texts_dir}")
            log_message(f"✅ Đã xóa thư mục: {raw_texts_dir}")
        except Exception as e:
            print(f"Lỗi khi xóa thư mục raw_texts: {e}")
            log_message(f"❌ Lỗi: {e}")
            messagebox.showerror("Lỗi", f"Không thể xóa thư mục raw_texts: {e}")

    if delete_texts:
        try:
            shutil.rmtree(texts_dir)
            print(f"Đã xóa thư mục {texts_dir}")
            log_message(f"✅ Đã xóa thư mục: {texts_dir}")
        except Exception as e:
            print(f"Lỗi khi xóa thư mục texts: {e}")
            messagebox.showerror("Lỗi", f"Không thể xóa thư mục texts: {e}")

    end_time_processing = time.time()
    total_time = end_time_processing - start_time_processing
    formatted_time = time.strftime("%H:%M:%S", time.gmtime(total_time))

    status_label.config(text=f"✅ Hoàn thành OCR {total_images} ảnh. Tổng thời gian: {formatted_time}")
    start_button.config(state=tk.NORMAL)
    VSF_button.config(state=tk.NORMAL)
    stop_button.config(state=tk.DISABLED)
    pause_button.config(state=tk.DISABLED, text="⏸ Tạm dừng")
    subtitle_button.config(state=tk.NORMAL)
    images_button.config(state=tk.NORMAL)
    log_message(f"✅ Hoàn thành OCR {total_images} hình ảnh.")
    log_message(f"✅ Thời gian xử lý OCR: {formatted_time}")


# Các hàm GUI (update_crop_values, set_entries_state, validate_float_input) giữ nguyên

# Hàm kiểm tra đầu vào chỉ là số và dấu chấm
def validate_float_input(action, value):
    if action != "1":  # Không phải hành động thêm ký tự
        return True
    return value.replace(".", "", 1).isdigit()

# Hàm thay đổi trạng thái của các ô nhập liệu
def set_entries_state(state):
    entry_crop_top.config(state=state)
    entry_crop_bottom.config(state=state)
    entry_crop_left.config(state=state)
    entry_crop_right.config(state=state)

# Hàm cập nhật giá trị crop dựa trên profile được chọn
def update_crop_values(event=None, top=None, bottom=None, left=None, right=None):
    """Cập nhật giá trị Crop vào các ô nhập liệu tương ứng"""
    # Lấy crop_profiles từ config
    _, _, _, _, _, threads, crop_profiles = load_config()
    if top is not None:
        crop_top_var.set(top)
    if bottom is not None:
        crop_bottom_var.set(bottom)
    if left is not None:
        crop_left_var.set(left)
    if right is not None:
        crop_right_var.set(right)

    selected_profile = profile_combobox.get()
    if selected_profile in crop_profiles:
        crop_top_var.set(crop_profiles[selected_profile]["top"])
        crop_bottom_var.set(crop_profiles[selected_profile]["bottom"])
        crop_left_var.set(crop_profiles[selected_profile]["left"])
        crop_right_var.set(crop_profiles[selected_profile]["right"])
        set_entries_state("readonly")
        toado_button.config(state=tk.DISABLED)
        VSF_button.config(state=tk.NORMAL)
    elif selected_profile == "Tuỳ chỉnh":
        set_entries_state("normal")
        toado_button.config(state=tk.NORMAL)
        VSF_button.config(state=tk.DISABLED)
    else:
        set_entries_state("readonly")

def on_start_button_click():
    """Xử lý sự kiện khi nút Start được click."""
    global STOP_FLAG, PAUSE_FLAG, LOG_FILE_PATH, subtitle_button, images_button, pause_button
    STOP_FLAG = False
    PAUSE_FLAG.set()  # Đảm bảo không ở trạng thái tạm dừng khi bắt đầu
    file_sub = subtitle_entry.get()
    images_dirr = images_entry.get()

    delete_raw_texts = delete_raw_texts_var.get()
    delete_texts = delete_texts_var.get()
    nen_raw_texts = nen_raw_texts_var.get()

    if not file_sub or not images_dirr:
        log_message("⚠️ Vui lòng nhập đầy đủ thông tin...")
        messagebox.showwarning("Cảnh báo", "Vui lòng nhập đầy đủ thông tin.")
        return

    # Lưu cấu hình vào file
    save_config(
        FOLDER_ID, delete_raw_texts, delete_texts, nen_raw_texts, videosubfinder_path, crop_profiles
    )

    LOG_FILE_PATH = file_sub if file_sub.endswith(".srt") else f"{file_sub}.srt"
    LOG_FILE_PATH = LOG_FILE_PATH.replace(".srt", ".log")

    # Ghi log
    with open(LOG_FILE_PATH, "w", encoding="utf-8") as log_file:
        log_file.write("=== STARTING NEW SESSION ===\n")

    log_message("🎬 Bắt đầu quá trình xử lý...")

    # Disable/Enable buttons
    start_button.config(state=tk.DISABLED)
    stop_button.config(state=tk.NORMAL)
    pause_button.config(state=tk.NORMAL, text="⏸ Tạm dừng")
    VSF_button.config(state=tk.DISABLED)
    subtitle_button.config(state=tk.DISABLED)
    images_button.config(state=tk.DISABLED)
    progress_bar["value"] = 0

    # Run OCR in a separate thread
    threading.Thread(
        target=start_processing,
        args=(
            file_sub,
            images_dirr,
            delete_raw_texts,
            delete_texts,
            nen_raw_texts,
            progress_callback,
        ),
    ).start()


def progress_callback():
    """Cập nhật thanh tiến trình."""
    global completed_scans, total_images, status_label

    progress_bar["value"] = (completed_scans / total_images) * 100
    status_label.config(text=f"✅ Đã OCR: {completed_scans}/{total_images}")
    root.update_idletasks()


def choose_images_directory(entry_images_dir):
    """Chọn thư mục hình ảnh."""
    images_dirr = filedialog.askdirectory(title="Chọn thư mục chứa hình ảnh")
    if images_dirr:
        entry_images_dir.delete(0, tk.END)
        entry_images_dir.insert(0, images_dirr)
        log_message(f"✅ Đường dẫn thư mục đã chọn: {images_dirr}")
    else:
        log_message("⚠️ Không có thư mục nào được chọn.")


def log_message(message):
    """Ghi log vào text widget và file."""
    global log_text, LOG_FILE_PATH
    log_text.config(state="normal")
    log_text.insert(tk.END, message + "\n")
    log_text.see(tk.END)
    log_text.config(state="disabled")
    root.update_idletasks()

    if LOG_FILE_PATH:
        with open(LOG_FILE_PATH, "a", encoding="utf-8") as log_file:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"[{timestamp}] {message}\n")


def create_gui():
    """Tạo giao diện người dùng."""
    global root, entry_subtitle, subtitle_entry, entry_images_dir, progress_bar, start_button, delete_raw_texts_var, delete_texts_var, nen_raw_texts_var, log_text, status_label, subtitle_button, images_button, create_txtimages_var

    log_frame = tk.Frame(root)
    log_frame.pack(pady=(0, 5), fill="both", expand=True)
    log_text = tk.Text(
        log_frame, height=5, wrap="word", state="disabled", bg="#0C0C0C", fg="#CCCCCC"
    )
    log_text.pack(fill="both", expand=True, padx=5, pady=5)


def choose_subtitle_file(entry_subtitle):
    """Chọn nơi lưu file phụ đề."""
    subtitle_file = filedialog.asksaveasfilename(
        defaultextension=".srt",
        filetypes=[("SRT files", "*.srt")],
        title="Lưu file phụ đề",
    )
    if subtitle_file:
        entry_subtitle.delete(0, tk.END)
        entry_subtitle.insert(0, subtitle_file)
        log_message(f"✅ Nơi lưu phụ đề: {subtitle_file}")
    else:
        log_message("⚠️ BẮT BUỘC PHẢI CHỌN LƯU PHỤ ĐỀ.")


def get_video_duration_opencv(video_path):
    """Lấy thời lượng video bằng OpenCV."""
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print("Không thể mở video.")
            return None

        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_seconds = total_frames / fps
        cap.release()

        timedelta_obj = datetime.timedelta(seconds=duration_seconds)

        # Định dạng timedelta thành "HH:MM:SS"
        hours, remainder = divmod(timedelta_obj.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        formatted_duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        return formatted_duration
    except Exception as e:
        print(f"Lỗi khi lấy thời lượng bằng OpenCV: {e}")
        return None


def choose_video_file(
    entry_video,
    entry_crop_left,
    entry_crop_right,
    entry_crop_top,
    entry_crop_bottom,
    subtitle_entry,
    progress_bar,
    root,
    log_message,
    videosubfinder_path,
    VSF_button,
    status_label,
    start_button,
    create_txtimages_var,
):
    """Chọn video và chạy VideoSubFinder, sử dụng video từ entry nếu đã có."""
    global DURATION
    selected_profile = profile_combobox.get()
    # Nếu profile không phải "Tuỳ chỉnh" hoặc entry_video rỗng, mở hộp thoại chọn video
    if selected_profile != "Tuỳ chỉnh" or not entry_video.get():
        video_file = filedialog.askopenfilename(
            filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv")],
            title="Chọn video để xử lý",
        )
        if not video_file:
            log_message("⚠️ Không có video nào được chọn.")
            return
        entry_video.delete(0, tk.END)
        entry_video.insert(0, video_file)
    else:
        video_file = entry_video.get()

    log_message(f"✅ Đã chọn video: {video_file}")

    # Lấy thời lượng video
    DURATION = get_video_duration_opencv(video_file)
    if DURATION:
        status_label.config(text=f"⏳ Thời lượng Video: | {DURATION}")
    else:
        status_label.config(text="⏳ Đang xử lý")

    subtitle_file = os.path.splitext(video_file)[0] + ".srt"
    subtitle_entry.delete(0, tk.END)
    subtitle_entry.insert(0, subtitle_file)

    try:
        crop_left = float(entry_crop_left.get())
        crop_right = float(entry_crop_right.get())
        crop_top = float(entry_crop_top.get())
        crop_bottom = float(entry_crop_bottom.get())
    except ValueError:
        log_message(
            "❌ Lỗi nhập liệu: Vui lòng nhập đúng giá trị số cho các tham số crop."
        )
        messagebox.showerror(
            "Lỗi nhập liệu", "Vui lòng nhập đúng giá trị số cho các tham số crop."
        )
        return

    if not os.path.exists(videosubfinder_path):
        log_message(f"❌ Lỗi: Không tìm thấy VideoSubFinder tại: {videosubfinder_path}")
        messagebox.showerror(
            "Lỗi", f"Không tìm thấy VideoSubFinder tại: {videosubfinder_path}"
        )
        return

    output_file = video_file.rsplit(".", 1)[0] + "_out"
    output_folder = "TXTImages" if create_txtimages_var.get() else "RGBImages"

    command = [
        videosubfinder_path,
        "-c",
        "-r",
    ] + (["-ccti"] if create_txtimages_var.get() else []) + [
        "-i",
        video_file,
        "-o",
        output_file,
        "-te",
        str(crop_top),
        "-be",
        str(crop_bottom),
        "-le",
        str(crop_left),
        "-re",
        str(crop_right),
    ]

    VSF_button.config(state=tk.DISABLED)
    start_button.config(state=tk.DISABLED)
    subtitle_button.config(state=tk.DISABLED)
    images_button.config(state=tk.DISABLED)

    def run_videosubfinder():
        """Chạy VideoSubFinder trong một thread riêng."""
        global images_dirr, rbg_images_folder, DURATION

        try:
            log_message(f"🚀 Đang chạy lệnh VideoSubFinder: {' '.join(command)}")

            video_output_folder = output_file
            images_folder = os.path.join(video_output_folder, output_folder)
            rbg_images_folder = os.path.join(video_output_folder, "RGBImages")

            #🟢 Bắt đầu giám sát ngay khi VideoSubFinder chạy
            log_message(f"👀 Bắt đầu giám sát thư mục RGBImages tại: {rbg_images_folder}")
            # Nếu thư mục chưa tồn tại, chờ nó được tạo
            threading.Thread(target=wait_for_rgbimages_and_monitor, args=[rbg_images_folder, DURATION]).start()

            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            # Sử dụng vòng lặp để đọc và cập nhật tiến trình
            while True:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break

                if line:
                    line = line.strip()
                    log_message(line)

                    match = re.search(r"%(\d+)", line)  # Tìm kiếm % theo sau là số
                    if match:
                        try:
                            percentage = int(match.group(1))
                            root.after(0, progress_bar.config, {"value": percentage})

                        except ValueError:
                            log_message("Lỗi chuyển đổi phần trăm")

            stderr_output = process.stderr.read()
            if stderr_output:
                log_message(f"❌ Lỗi VideoSubFinder: {stderr_output}")
                root.after(
                    0,
                    lambda: messagebox.showerror(
                        "Lỗi", f"Quá trình xử lý video thất bại: {stderr_output}"
                    ),
                )
                root.after(
                    0, status_label.config, {"text": "❌ Lỗi!"}
                )  # Cập nhật trạng thái lỗi

            returncode = process.wait()

            if returncode != 0:
                log_message(f"\n✅ VideoSubFinder đã hoàn tất xử lý ảnh từ Video")
                root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "Thông báo", f"VideoSubFinder đã hoàn tất xử lý ảnh từ Video"
                    ),
                )
                root.after(
                    0, status_label.config, {"text": f"Đã xử lý xong Video! | 📂 Tổng ảnh: {EVENT_HANDLER.file_count}"}
                )  # Cập nhật trạng thái lỗi
            else:
                log_message("✅ Quá trình xử lý video đã hoàn tất.")
                root.after(
                    0, status_label.config, {"text": "✅ Hoàn thành!"}
                )  # Cập nhật trạng thái hoàn thành

            if os.path.exists(images_folder):
                root.after(0, lambda: images_entry.delete(0, "end"))
                root.after(0, lambda: images_entry.insert(0, images_folder))
                images_dirr = images_folder
        except Exception as e:
            log_message(f"❌ Lỗi trong run_videosubfinder: {e}")
        finally:
            root.after(0, VSF_button.config, {"state": tk.NORMAL})
            root.after(0, start_button.config, {"state": tk.NORMAL})
            root.after(0, subtitle_button.config, {"state": tk.NORMAL})
            root.after(0, images_button.config, {"state": tk.NORMAL})

    threading.Thread(target=run_videosubfinder, daemon=True).start()


def choose_video_for_crop():
    video_file = entry_video.get()
    if not video_file or not os.path.exists(video_file):
        video_file = filedialog.askopenfilename(
            filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv")],
            title="Chọn video để xử lý",
        )
        if not video_file:
            log_message("⚠️ Không có video nào được chọn.")
            return
        entry_video.delete(0, tk.END)
        entry_video.insert(0, video_file)

    def update_crop_values_callback(top=None, bottom=None, left=None, right=None):
        if top is not None:
            crop_top_var.set(top)
        if bottom is not None:
            crop_bottom_var.set(bottom)
        if left is not None:
            crop_left_var.set(left)
        if right is not None:
            crop_right_var.set(right)
        set_entries_state("normal")
        toado_button.config(state=tk.NORMAL)
        VSF_button.config(state=tk.NORMAL)
        profile_combobox.set("Tuỳ chỉnh")

    CropSelector(
        root,
        video_file,
        update_crop_values_callback,
        entry_video,
        entry_crop_left,
        entry_crop_right,
        entry_crop_top,
        entry_crop_bottom,
        subtitle_entry,
        progress_bar,
        root,
        log_message,
        videosubfinder_path,
        VSF_button,
        status_label,
        start_button,
        create_txtimages_var,
    )


class CropSelector:
    def __init__(
        self,
        root_main,
        video_path,
        update_crop_callback,
        entry_video,
        entry_crop_left,
        entry_crop_right,
        entry_crop_top,
        entry_crop_bottom,
        subtitle_entry,
        progress_bar,
        root_app,
        log_message,
        videosubfinder_path,
        VSF_button,
        status_label,
        start_button,
        create_txtimages_var,
    ):
        self.root_main = root_main
        self.video_path = video_path
        self.update_crop_callback = update_crop_callback
        self.entry_video = entry_video
        self.entry_crop_left = entry_crop_left
        self.entry_crop_right = entry_crop_right
        self.entry_crop_top = entry_crop_top
        self.entry_crop_bottom = entry_crop_bottom
        self.subtitle_entry = subtitle_entry
        self.progress_bar = progress_bar
        self.root_app = root_app
        self.log_message = log_message
        self.videosubfinder_path = videosubfinder_path
        self.VSF_button = VSF_button
        self.status_label = status_label
        self.start_button = start_button
        self.create_txtimages_var = create_txtimages_var

        self.cap = None
        self.current_frame_index = 0
        self.total_frames = 0
        self.video_width = 0
        self.video_height = 0
        self.canvas_width = 0
        self.canvas_height = 0
        self.top_line_y = 0
        self.bottom_line_y = 0
        self.left_line_x = 0
        self.right_line_x = 0
        self.selected_line = None

        self.root = tk.Toplevel(self.root_main)
        self.root.title("Chọn vùng crop")
        self.root.geometry("1000x800")

        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(main_frame, bg="black")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.top_var = tk.StringVar()
        self.bottom_var = tk.StringVar()
        self.left_var = tk.StringVar()
        self.right_var = tk.StringVar()

        self.canvas.bind("<Button-1>", self.on_mouse_press)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_release)
        self.canvas.bind("<Motion>", self.on_mouse_move)

        # Frame for parameters
        param_frame = tk.Frame(main_frame)
        param_frame.pack(fill=tk.X, pady=5)

        tk.Label(param_frame, text="Top:").pack(side=tk.LEFT, padx=5)
        tk.Entry(param_frame, textvariable=self.top_var, width=10, state="readonly",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=2)

        tk.Label(param_frame, text="Bottom:").pack(side=tk.LEFT, padx=5)
        tk.Entry(param_frame, textvariable=self.bottom_var, width=10, state="readonly",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=2)

        tk.Label(param_frame, text="Left:").pack(side=tk.LEFT, padx=5)
        tk.Entry(param_frame, textvariable=self.left_var, width=10, state="readonly",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=2)

        tk.Label(param_frame, text="Right:").pack(side=tk.LEFT, padx=5)
        tk.Entry(param_frame, textvariable=self.right_var, width=10, state="readonly",
                 font=("Consolas", 10, "bold")).pack(side=tk.LEFT, padx=2)

        ttk.Button(param_frame, text="Xác nhận vùng chọn", command=self.confirm_selection).pack(side=tk.LEFT, padx=5)

        # Timeline slider (Horizontal Scale)
        timeline_frame = tk.Frame(main_frame)  # Created new frame to house both slider and time
        timeline_frame.pack(fill=tk.X, pady=5)

        # Timeline slider (Horizontal Scale)
        self.slider = ttk.Scale(timeline_frame, orient="horizontal", command=self.seek_video)
        self.slider.pack(side=tk.LEFT, expand=True, fill=tk.X)

        # Time label
        self.time_label = tk.Label(timeline_frame, text="00:00:00.000", font=("Helvetica", 10))
        self.time_label.pack(side=tk.LEFT, padx=2)
        ttk.Button(timeline_frame, text=">>1giây", command=self.fast_forward_1s, width=8).pack(side=tk.LEFT, padx=5)

        self.root.after(100, self.load_video)

    def load_video(self):
        """Tải video và thiết lập kích thước phù hợp với cửa sổ"""
        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            messagebox.showerror("Error", f"Could not open video file at {self.video_path}")
            return

        # Lấy kích thước gốc của video
        self.video_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.video_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # Lấy kích thước cửa sổ
        self.root.update_idletasks()  # Cập nhật UI trước khi lấy kích thước
        window_width = self.root.winfo_width()
        window_height = self.root.winfo_height()

        # Tính toán tỷ lệ co để video vừa với cửa sổ
        scale_ratio = min(window_width / self.video_width, window_height / self.video_height)

        # Cập nhật kích thước Canvas dựa trên tỷ lệ co
        self.canvas_width = int(self.video_width * scale_ratio)
        self.canvas_height = int(self.video_height * scale_ratio)
        self.canvas.config(width=self.canvas_width, height=self.canvas_height)

        # Khởi tạo các toạ độ đường kẻ
        if self.video_width > 0 and self.video_height > 0:
            self.top_line_y = int(0.8574 * self.video_height)
            self.bottom_line_y = int(0.9898 * self.video_height)
            self.left_line_x = int(0.1 * self.video_width)
            self.right_line_x = int(0.9 * self.video_width)
        else:
            self.top_line_y, self.bottom_line_y, self.left_line_x, self.right_line_x = 0, 0, 0, 0
        # Lấy tổng số frame và thiết lập thanh trượt
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.slider.config(to=self.total_frames - 1)
        self.current_frame_index = 0

        self.show_frame()  # Hiển thị frame đầu tiên

    def seek_video(self, value):
        """Seeks to a particular frame based on scale's value."""
        frame_index = int(float(value))
        if frame_index != self.current_frame_index:
            self.current_frame_index = frame_index
            self.show_frame()

    def fast_forward_1s(self):
        """Fast forward the video by 1 seconds."""
        if not self.cap:
            return

        # Calculate the current time in seconds
        current_time = self.current_frame_index / self.cap.get(cv2.CAP_PROP_FPS)

        # Add 10 seconds to the current time
        new_time = current_time + 1

        # Calculate the new frame index
        new_frame_index = int(new_time * self.cap.get(cv2.CAP_PROP_FPS))

        # Ensure the new frame index is within bounds
        if new_frame_index >= self.total_frames:
            new_frame_index = self.total_frames - 1

        # Update the current frame index and seek to the new frame
        self.current_frame_index = new_frame_index
        self.slider.set(new_frame_index)
        self.show_frame()

    def show_frame(self):
        """Hiển thị frame video lên canvas với kích thước phù hợp"""
        if not self.cap or not self.cap.isOpened():
            return

        # Đảm bảo Canvas đã cập nhật kích thước
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_index)
        ret, frame = self.cap.read()
        if not ret:
            return

        # Resize frame để vừa với kích thước Canvas
        if self.video_width > 0 and self.video_height > 0:
            frame = cv2.resize(frame, (self.canvas_width, self.canvas_height))
        self.frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        img = Image.fromarray(self.frame)
        self.photo = ImageTk.PhotoImage(image=img)

        self.canvas.delete("all")  # Xóa hình cũ trước khi vẽ
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        # Vẽ lại bounding box nếu có
        self.draw_bounding_lines()

        # Cập nhật thời gian
        current_time = self.current_frame_index / self.cap.get(cv2.CAP_PROP_FPS)
        self.update_time_display(current_time)

    def on_mouse_press(self, event):
        # Chuyển đổi tọa độ chuột trên canvas về tọa độ trên video gốc
        x = int(event.x * (self.video_width / self.canvas_width)) if self.canvas_width > 0 else event.x
        y = int(event.y * (self.video_height / self.canvas_height)) if self.canvas_height > 0 else event.y
        self.selected_line = self.get_clicked_line(x, y)

    def get_clicked_line(self, x, y):
        """Determine which line is selected."""
        tolerance = 5  # Adjust as needed
        if abs(y - self.top_line_y) < tolerance:
            return "top"
        elif abs(y - self.bottom_line_y) < tolerance:
            return "bottom"
        elif abs(x - self.left_line_x) < tolerance:
            return "left"
        elif abs(x - self.right_line_x) < tolerance:
            return "right"
        else:
            return None

    def on_mouse_drag(self, event):
        """Draws a rectangle during mouse drag."""
        # Chuyển đổi tọa độ chuột trên canvas về tọa độ trên video gốc
        x = int(event.x * (self.video_width / self.canvas_width)) if self.canvas_width > 0 else event.x
        y = int(event.y * (self.video_height / self.canvas_height)) if self.canvas_height > 0 else event.y

        if self.selected_line == "top":
            self.top_line_y = y
            self.top_line_y = max(0, min(self.top_line_y, self.bottom_line_y))  # Keep within bounds
        elif self.selected_line == "bottom":
            self.bottom_line_y = y
            self.bottom_line_y = max(self.top_line_y, min(self.bottom_line_y, self.video_height))  # Keep within bounds
        elif self.selected_line == "left":
            self.left_line_x = x
            self.left_line_x = max(0, min(self.left_line_x, self.right_line_x))  # Keep within bounds
        elif self.selected_line == "right":
            self.right_line_x = x
            self.right_line_x = max(self.left_line_x, min(self.right_line_x, self.video_width))  # Keep within bounds

        self.draw_bounding_lines()
        self.update_parameters()

    def on_mouse_release(self, event):
        """Finalize the drawing of rectangle and update parameter."""
        self.selected_line = None
        self.canvas.config(cursor="arrow")  # Reset cursor

    def update_parameters(self):
        """Calculates and updates the parameter variables based on selected rectangle"""
        if not self.video_width or not self.video_height:
            return

        # Calculate the cropping percentages:
        top_crop = 1 - (self.top_line_y / self.video_height)
        bottom_crop = (self.video_height - self.bottom_line_y) / self.video_height
        left_crop = self.left_line_x / self.video_width
        right_crop = self.right_line_x / self.video_width

        self.top_var.set(f"{top_crop:.4f}")
        self.bottom_var.set(f"{bottom_crop:.4f}")
        self.left_var.set(f"{left_crop:.4f}")
        self.right_var.set(f"{right_crop:.4f}")

    def update_time_display(self, current_time):
        """Formats and updates the time display label."""
        time_obj = datetime.datetime.min + datetime.timedelta(seconds=current_time)
        time_str = time_obj.strftime("%H:%M:%S.%f")[:-3]  # format the time to HH:MM:SS.MS
        self.time_label.config(text=time_str)

    def draw_bounding_lines(self):
        """Draws bounding lines on canvas based on the coordinates"""
        if not self.video_width or not self.video_height:
            return

        # Chuyển đổi tọa độ video gốc sang tọa độ canvas để vẽ đường viền
        canvas_top_y = int(self.top_line_y * (self.canvas_height / self.video_height)) if self.canvas_height > 0 and self.video_height > 0 else self.top_line_y
        canvas_bottom_y = int(self.bottom_line_y * (self.canvas_height / self.video_height)) if self.canvas_height > 0 and self.video_height > 0 else self.bottom_line_y
        canvas_left_x = int(self.left_line_x * (self.canvas_width / self.video_width)) if self.canvas_width > 0 and self.video_width > 0 else self.left_line_x
        canvas_right_x = int(self.right_line_x * (self.canvas_width / self.video_width)) if self.canvas_width > 0 and self.video_width > 0 else self.right_line_x

        # XÓA CÁC ĐƯỜNG KẺ CŨ TRƯỚC KHI VẼ MỚI
        self.canvas.delete("bounding_lines")

        # Vẽ các đường viền với tag "bounding_lines"
        self.top_line_id = self.canvas.create_line(0, canvas_top_y, self.canvas_width, canvas_top_y, fill="yellow",
                                                    width=2, tags="bounding_lines")
        self.bottom_line_id = self.canvas.create_line(0, canvas_bottom_y, self.canvas_width, canvas_bottom_y,
                                                      fill="yellow", width=2, tags="bounding_lines")
        self.left_line_id = self.canvas.create_line(canvas_left_x, 0, canvas_left_x, self.canvas_height,
                                                   fill="yellow", width=2, tags="bounding_lines")
        self.right_line_id = self.canvas.create_line(canvas_right_x, 0, canvas_right_x, self.canvas_height,
                                                    fill="yellow", width=2, tags="bounding_lines")

    def confirm_selection(self):
        """Xác nhận vùng chọn và cập nhật giá trị Crop ở màn hình chính"""
        top_crop = float(self.top_var.get())
        bottom_crop = float(self.bottom_var.get())
        left_crop = float(self.left_var.get())
        right_crop = float(self.right_var.get())

        # Gọi hàm cập nhật giá trị Crop ở màn hình chính
        self.update_crop_callback(
            top=top_crop,
            bottom=bottom_crop,
            left=left_crop,
            right=right_crop
        )
        
        # Kiểm tra nếu profile là "Tùy chỉnh" thì chạy VSF
        if profile_combobox.get() == "Tuỳ chỉnh":
            # Sử dụng video_path đã chọn thay vì gọi lại hộp thoại
            self.entry_video.delete(0, tk.END)
            self.entry_video.insert(0, self.video_path)
            choose_video_file(
                self.entry_video,
                self.entry_crop_left,
                self.entry_crop_right,
                self.entry_crop_top,
                self.entry_crop_bottom,
                self.subtitle_entry,
                self.progress_bar,
                self.root_main,
                self.log_message,
                self.videosubfinder_path,
                self.VSF_button,
                self.status_label,
                self.start_button,
                self.create_txtimages_var
            )
            
        self.root.destroy()

    def on_mouse_move(self, event):
        """Changes the cursor when the mouse is over a bounding line."""
        # Chuyển đổi tọa độ chuột trên canvas về tọa độ trên video gốc
        x = int(event.x * (self.video_width / self.canvas_width)) if self.canvas_width > 0 else event.x
        y = int(event.y * (self.video_height / self.canvas_height)) if self.canvas_height > 0 else event.y

        line = self.get_clicked_line(x, y)
        if line in ("top", "bottom"):
            self.canvas.config(cursor="sb_v_double_arrow")  # Vertical double arrow
        elif line in ("left", "right"):
            self.canvas.config(cursor="sb_h_double_arrow")  # Horizontal double arrow
        else:
            self.canvas.config(cursor="arrow")  # Default cursor

# =============================================================================================
root = tk.Tk()
root.title("SEGG OCR Tool v1.36_Optimizer")
window_width = 622
window_height = 578
# Lấy kích thước màn hình
screen_width = root.winfo_screenwidth()
screen_height = root.winfo_screenheight()

# Tính toán vị trí để căn giữa
x = (screen_width // 2) - (window_width // 2)
y = (screen_height // 2) - (window_height // 2)

# Đặt kích thước và vị trí cửa sổ
root.geometry(f"{window_width}x{window_height}+{x}+{y}")
button_width = 20

# Tên phụ đề
subtitle_frame = tk.Frame(root)
subtitle_frame.pack(pady=5, fill="x")  # Đặt frame để chứa các widget trên cùng một hàng
tk.Label(subtitle_frame, text="Tên file phụ đề:").pack(side="left", padx=5)
subtitle_entry = tk.Entry(subtitle_frame, width=58)  # Giảm kích thước width cho phù hợp
subtitle_entry.pack(side="left", padx=5)

subtitle_button = tk.Button(
    subtitle_frame,
    text="📂 Chọn nơi lưu sub",
    width=button_width,
    command=lambda: choose_subtitle_file(subtitle_entry),
)
subtitle_button.pack(side="right", padx=5)

# Thư mục hình ảnh
images_frame = tk.Frame(root)
images_frame.pack(pady=5, fill="x")  # Đặt frame để chứa các widget trên cùng một hàng
tk.Label(images_frame, text="Thư mục ảnh:").pack(side="left", padx=5)
images_entry = tk.Entry(images_frame, width=59)  # Giảm width cho Entry để vừa khung
images_entry.pack(side="left", padx=5)

images_button = tk.Button(
    images_frame,
    text="📂 Chọn thư mục ảnh",
    width=button_width,
    command=lambda: choose_images_directory(images_entry),
)
images_button.pack(side="right", padx=5)

# Khung nhập video
video_frame = tk.Frame(root)
video_frame.pack(
    pady=(0, 1), fill="x"
)  # Đặt frame để chứa các widget trên cùng một hàng
tk.Label(video_frame, text="Tệp tin video:").pack(side="left", padx=5)

entry_video = tk.Entry(video_frame, width=59)  # Giảm width cho Entry để vừa khung
entry_video.pack(side="left", padx=5)

# Tùy chọn crop video
crop_frame = tk.Frame(root)
crop_frame.pack(padx=10, pady=5, fill="x")

# Thêm nhãn và ô nhập liệu cho crop
crop_label = tk.Label(crop_frame, text="Crop (Top, Bottom, Left, Right):")
crop_label.pack(side="left", padx=5)

# Các biến lưu giá trị crop
crop_top_var = tk.StringVar(value="0")
crop_bottom_var = tk.StringVar(value="0")
crop_left_var = tk.StringVar(value="0")
crop_right_var = tk.StringVar(value="0")

validate_command = (root.register(validate_float_input), "%d", "%P")

entry_crop_top = tk.Entry(
    crop_frame,
    textvariable=crop_top_var,
    width=10,
    state="readonly",
    validate="key",
    validatecommand=validate_command,
)
entry_crop_top.pack(side="left", padx=2)

entry_crop_bottom = tk.Entry(
    crop_frame,
    textvariable=crop_bottom_var,
    width=10,
    state="readonly",
    validate="key",
    validatecommand=validate_command,
)
entry_crop_bottom.pack(side="left", padx=2)

entry_crop_left = tk.Entry(
    crop_frame,
    textvariable=crop_left_var,
    width=10,
    state="readonly",
    validate="key",
    validatecommand=validate_command,
)
entry_crop_left.pack(side="left", padx=2)

entry_crop_right = tk.Entry(
    crop_frame,
    textvariable=crop_right_var,
    width=10,
    state="readonly",
    validate="key",
    validatecommand=validate_command,
)
entry_crop_right.pack(side="left", padx=2)

# Thêm Combobox để chọn profile
profile_combobox = ttk.Combobox(
    crop_frame,
    values=["Chọn profile", "vlxx, javhd", "sextop", "phimKK", "titdam", "Tuỳ chỉnh"],
    state="readonly",
)
profile_combobox.pack(side="left", padx=5)
profile_combobox.set("Chọn profile")  # Đặt giá trị mặc định
profile_combobox.bind("<<ComboboxSelected>>", update_crop_values)

(
    FOLDER_ID,
    delete_raw_texts,
    delete_texts,
    nen_raw_texts,
    videosubfinder_path,
    threads,
    crop_profiles,
) = load_config()
# Checkbox Tùy chọn
delete_raw_texts_var = tk.BooleanVar(value=True if delete_raw_texts else False)
delete_texts_var = tk.BooleanVar(value=True if delete_texts else False)
nen_raw_texts_var = tk.BooleanVar(value=True if nen_raw_texts else False)
create_txtimages_var = tk.BooleanVar(value=False)
delete_options_frame = tk.Frame(root)
delete_options_frame.pack(pady=(0, 1), fill="x")  # Đặt frame

# Checkbox cho tùy chọn xóa thư mục raw_texts
tk.Checkbutton(
    delete_options_frame,
    text="Xóa folder raw_texts khi xong",
    variable=delete_raw_texts_var,
    anchor="w",
).pack(side="left", padx=5)

# Checkbox cho tùy chọn xóa thư mục texts
tk.Checkbutton(
    delete_options_frame,
    text="Xóa folder texts khi xong",
    variable=delete_texts_var,
    anchor="w",
).pack(side="left", padx=5)

# Checkbox cho tùy chọn nén thư mục raw_texts
tk.Checkbutton(
    delete_options_frame,
    text="Nén folder raw_texts",
    variable=nen_raw_texts_var,
    anchor="w",
).pack(side="left", padx=5)

# Checkbox cho tùy chọn tạo TXTImages
tk.Checkbutton(
    delete_options_frame,
    text="Tạo TXTImages",
    variable=create_txtimages_var,
    anchor="w",
).pack(side="left", padx=5)

# Tạo Frame chứa các nút Bắt đầu và Dừng
button_frame = tk.Frame(root)
button_frame.pack(pady=(0, 2), fill="x")
# Thêm nút Bắt đầu vào Frame
start_button = tk.Button(
    button_frame,
    text="🎬 Bắt đầu OCR",
    width=12,
    command=on_start_button_click,
    bg="#F50398",
)
start_button.pack(side="left", padx=5)
# Thêm nút Dừng vào Frame
stop_button = tk.Button(
    button_frame,
    text="❌ Dừng OCR",
    width=11,
    command=stop_processing,
    state=tk.DISABLED,
)
stop_button.pack(side="left", padx=2)

# Thêm nút Tạm dừng vào Frame
pause_button = tk.Button(
    button_frame,
    text="⏸ Tạm dừng",
    width=11,
    command=toggle_pause,
    state=tk.DISABLED,
)
pause_button.pack(side="left", padx=2)

status_label = tk.Label(button_frame, text="Trạng thái chương trình: Sẵn sàng", fg="red")
status_label.pack(side="right", padx=2)  # Sắp xếp Label trạng thái gần nút

toado_button = tk.Button(
    video_frame,
    text="✨ Toạ độ",
    width=8,
    command=choose_video_for_crop,
)
toado_button.pack(side="right", padx=5)

VSF_button = tk.Button(
    video_frame,
    text="🚀 Chạy VSF",
    width=9,
    command=lambda: choose_video_file(
        entry_video,
        entry_crop_left,
        entry_crop_right,
        entry_crop_top,
        entry_crop_bottom,
        subtitle_entry,
        progress_bar,  # Thêm progress_bar
        root,  # Thêm root
        log_message,  # Thêm log_message
        videosubfinder_path,
        VSF_button,
        status_label,
        start_button,
        create_txtimages_var,
    ),
)
VSF_button.pack(side="right", padx=5)

progress_bar = ttk.Progressbar(
    root, orient="horizontal", length=612, mode="determinate"
)
progress_bar.pack(pady=(1, 0))

def on_exit():
    """Xác nhận trước khi thoát chương trình."""
    if messagebox.askokcancel("Thoát", "Bạn có chắc muốn thoát chương trình?"):
        root.destroy()
        sys.exit()

if __name__ == "__main__":
    (
        FOLDER_ID,
        delete_raw_texts,
        delete_texts,
        compress_raw_texts,
        videosubfinder_path,
        threads,
        crop_profiles,
    ) = load_config()
    create_gui()
    toado_button.config(state=tk.DISABLED)
    log_message(
        "|====================CHÚ Ý QUAN TRỌNG====================|\n\nVui lòng cấu hình trước API bằng tài khoản google của bạn.\nSau đó tải credentials.json đặt cùng thư mục chương trình.\n\n|=====================================Discord: ePubc#9826|\n"
    )
    # Gắn sự kiện đóng cửa sổ vào hàm xác nhận
    root.protocol("WM_DELETE_WINDOW", on_exit)
    root.mainloop()