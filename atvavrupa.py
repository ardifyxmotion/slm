import os
import glob
import subprocess
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

STREAM_DIR = "streams"
M3U8_FILENAME = os.path.join(STREAM_DIR, "atvavrupa.m3u8")
MAX_SEGMENTS = 0  # 0 = sınırsız; playlist yeni segmentlerle büyür

# GitHub Raw URL
BASE_URL = "https://raw.githubusercontent.com/ardifyxmotion/slm/main/streams/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    )
}


def download_segment(args):
    fname, url = args
    fpath = os.path.join(STREAM_DIR, fname)

    # Segment zaten varsa tekrar indirme.
    if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fname

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=20
        )
        response.raise_for_status()

        if not response.content:
            print(f"Boş segment: {fname}")
            return None

        tmp_path = fpath + ".tmp"

        with open(tmp_path, "wb") as f:
            f.write(response.content)

        os.replace(tmp_path, fpath)

        return fname

    except requests.RequestException as e:
        print(f"Segment indirilemedi: {fname} -> {e}")
        return None

    except OSError as e:
        print(f"Dosya yazılamadı: {fname} -> {e}")
        return None


def get_stream_url():
    command = [
        "streamlink",
        "--stream-url",
        "https://www.atvavrupa.tv/canli-yayin",
        "best",
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60
    )

    if result.returncode != 0:
        print("Streamlink hatası:")
        print(result.stderr)
        return None

    urls = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip().startswith(("http://", "https://"))
    ]

    if not urls:
        return None

    return urls[-1]


def parse_playlist(playlist, stream_url):
    """
    Kaynak M3U8 içindeki MEDIA-SEQUENCE, segment URL'leri
    ve gerçek EXTINF sürelerini çıkarır.
    """
    lines = playlist.splitlines()

    segments = []
    current_duration = None
    media_sequence = 0

    for line in lines:
        line = line.strip()

        if not line:
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except (ValueError, IndexError):
                media_sequence = 0

        elif line.startswith("#EXTINF:"):
            try:
                duration_text = line.split(":", 1)[1].split(",", 1)[0]
                current_duration = float(duration_text)
            except (ValueError, IndexError):
                current_duration = None

        elif not line.startswith("#"):
            if line.startswith(("http://", "https://")):
                segment_url = line
            else:
                segment_url = requests.compat.urljoin(
                    stream_url,
                    line
                )

            duration = (
                current_duration
                if current_duration is not None
                else 10.0
            )

            segments.append((segment_url, duration))
            current_duration = None

    return media_sequence, segments


def cleanup_segments(valid_names):
    """
    Kalıcı DVR için eski segmentleri silme.
    M3U8 geçmişi büyümeye devam eder.
    """
    return

def write_m3u8(valid_files, media_sequence):
    """
    Biriken tüm DVR segmentlerini M3U8 olarak yazar.
    Playlist EVENT olarak kalır ve yeni segmentler eklendikçe uzar.
    """
    if not valid_files:
        return

    target_duration = max(
        1,
        int(max(item[2] for item in valid_files) + 0.999)
    )

    with open(M3U8_FILENAME, "w", encoding="utf-8", newline="\\n") as f:
        f.write("#EXTM3U\\n")
        f.write("#EXTVLCOPT:http-referrer=https://www.atvavrupa.tv/webtv/canli-yayin\\n")
        f.write("#EXT-X-VERSION:3\\n")
        f.write("#EXT-X-PLAYLIST-TYPE:EVENT\\n")
        f.write(f"#EXT-X-TARGETDURATION:{target_duration}\\n")
        f.write(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}\\n")

        for fname, _, duration in valid_files:
            f.write(f"#EXTINF:{duration:.3f},\\n")
            f.write(f"{BASE_URL}{fname}\\n")

def main():
    os.makedirs(
        STREAM_DIR,
        exist_ok=True
    )

    print("ATV Avrupa M3U8 senkronizasyonu başlıyor...")

    # 1. Canlı yayın M3U8 adresini Streamlink ile bul.
    try:
        stream_url = get_stream_url()
    except subprocess.TimeoutExpired:
        print("Streamlink zaman aşımına uğradı.")
        return
    except Exception as e:
        print(
            f"Canlı yayın URL'si alınamadı: {e}"
        )
        return

    if not stream_url:
        print(
            "Canlı yayın M3U8 adresi bulunamadı."
        )
        return

    print(f"Kaynak M3U8: {stream_url}")

    # 2. Kaynak M3U8'i indir.
    try:
        response = requests.get(
            stream_url,
            headers=HEADERS,
            timeout=20
        )
        response.raise_for_status()
        playlist = response.text

    except requests.RequestException as e:
        print(
            f"Kaynak M3U8 indirilemedi: {e}"
        )
        return

    # 3. Segmentleri çıkar.
    source_media_sequence, segments = parse_playlist(
        playlist,
        stream_url
    )

    if not segments:
        print(
            "Kaynak M3U8 içinde segment bulunamadı."
        )
        return

    print(
        f"Kaynak playlist segment sayısı: "
        f"{len(segments)}"
    )

    # Kaynaktaki tüm segmentleri kullan.
    # Aynı MEDIA-SEQUENCE numarası daha önce indirildiyse dosya tekrar indirilmez.
    start_sequence = source_media_sequence

    target_files = []

    for index, (url, duration) in enumerate(segments):
        sequence = start_sequence + index
        fname = f"seg_{sequence}.ts"

        target_files.append(
            (fname, url, duration)
        )

    # 4. Segmentleri paralel indir.
    download_jobs = [
        (fname, url)
        for fname, url, _ in target_files
    ]

    print(
        f"{len(download_jobs)} segment indiriliyor..."
    )

    with ThreadPoolExecutor(
        max_workers=10
    ) as executor:

        results = list(
            executor.map(
                download_segment,
                download_jobs
            )
        )

    # 5. Başarıyla indirilen segmentleri seç.
    valid_files = []

    for result, item in zip(
        results,
        target_files
    ):
        fname, url, duration = item
        fpath = os.path.join(
            STREAM_DIR,
            fname
        )

        if (
            result == fname
            and os.path.exists(fpath)
            and os.path.getsize(fpath) > 0
        ):
            valid_files.append(
                (fname, url, duration)
            )

    if not valid_files:
        print(
            "Hiçbir segment başarıyla indirilemedi."
        )
        return

    print(
        f"Başarıyla indirilen segment: "
        f"{len(valid_files)}"
    )

    # 6. Mevcut DVR segmentlerini de playlist'e dahil et.
    # Böylece her GitHub Actions çalışmasında playlist baştan 300 segmente dönmez.
    existing = []

    for fpath in glob.glob(os.path.join(STREAM_DIR, "seg_*.ts")):
        fname = os.path.basename(fpath)

        try:
            sequence = int(Path(fname).stem.split("_", 1)[1])
        except (ValueError, IndexError):
            continue

        if os.path.getsize(fpath) <= 0:
            continue

        existing.append((sequence, fname))

    # Kaynak playlistteki süre bilgisini kullanabilmek için mevcut playlisti oku.
    duration_map = {}
    if os.path.exists(M3U8_FILENAME):
        try:
            with open(M3U8_FILENAME, "r", encoding="utf-8") as old_m3u:
                old_lines = old_m3u.read().splitlines()

            old_seq = None
            old_duration = None

            for line in old_lines:
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        old_seq = int(line.split(":", 1)[1])
                    except ValueError:
                        old_seq = None
                elif line.startswith("#EXTINF:"):
                    try:
                        old_duration = float(line.split(":", 1)[1].split(",", 1)[0])
                    except ValueError:
                        old_duration = None
                elif line.startswith("seg_") and line.endswith(".ts"):
                    try:
                        seq = int(Path(line).stem.split("_", 1)[1])
                    except (ValueError, IndexError):
                        seq = None

                    if seq is not None and old_duration is not None:
                        duration_map[seq] = old_duration

                    old_duration = None

        except OSError:
            pass

    # Yeni segment sürelerini mevcut süre haritasına ekle.
    for fname, _, duration in valid_files:
        try:
            seq = int(Path(fname).stem.split("_", 1)[1])
            duration_map[seq] = duration
        except (ValueError, IndexError):
            continue

    # Tüm mevcut segmentleri sıralı şekilde birleştir.
    all_sequences = sorted(
        {
            seq for seq, _ in existing
            if seq in duration_map
        }
        |
        {
            int(Path(fname).stem.split("_", 1)[1])
            for fname, _, _ in valid_files
        }
    )

    accumulated_files = []

    for seq in all_sequences:
        fname = f"seg_{seq}.ts"
        fpath = os.path.join(STREAM_DIR, fname)

        if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
            duration = duration_map.get(seq)
            if duration is not None:
                accumulated_files.append((fname, "", duration))

    if not accumulated_files:
        print("Birleştirilecek DVR segmenti bulunamadı.")
        return

    # Eski segmentleri SİLME: DVR geçmişi korunur.
    cleanup_segments({item[0] for item in accumulated_files})

    # 7. Biriken tüm segmentlerle M3U8 oluştur.
    playlist_sequence = all_sequences[0]
    write_m3u8(accumulated_files, playlist_sequence)

    total_duration = sum(item[2] for item in accumulated_files)
    print(
        f"DVR playlist toplam süresi: {total_duration / 60:.2f} dakika "
        f"({len(accumulated_files)} segment)"
    )

    print(
        f"M3U8 oluşturuldu: {M3U8_FILENAME}"
    )


if __name__ == "__main__":
    main()
