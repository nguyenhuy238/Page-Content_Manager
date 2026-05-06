# FB Page Manager

Dashboard và scheduler tự động cho Facebook Page:
- Thu thập bài từ RSS/NewsAPI
- Tạo caption bằng Gemini
- Đưa vào hàng đợi và đăng theo lịch
- Theo dõi thống kê trong SQLite
- Pipeline campaign cho thị trường Mexico:
  - Lấy nguồn từ YouTube channels + URL bài báo
  - Trích transcript/tóm tắt
  - Viết gói nội dung tiếng Tây Ban Nha (WordPress + Facebook)
  - (Tùy chọn) tạo ảnh AI minh họa
  - (Tùy chọn) đăng web và Facebook + auto comment kéo traffic

## Cài đặt (3 lệnh)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Cấu hình .env

```bash
copy .env.example .env
```

Mở `.env` và điền tối thiểu:
- `PAGE_ID`
- `ACCESS_TOKEN`
- `GEMINI_API_KEY`
- `RSS_URLS`
- `POSTING_TIMES`

## Chạy web dashboard

```bash
python run.py web
```

Mặc định mở ở `http://localhost:5000` (đổi qua `FLASK_PORT`).

## Chạy scheduler 24/7

```bash
python run.py scheduler
```

Scheduler đọc `POSTING_TIMES` và mỗi slot sẽ lấy bài queued tiếp theo để đăng.

## Chạy fetch một lần

```bash
python run.py fetch
```

## Chạy campaign tự động (YouTube + News URLs + WP + FB)

Dry-run mặc định (không đăng thật):

```bash
python run.py campaign --limit 4
```

Chạy live (đăng thật nếu đã bật các cờ auto-post trong `.env`):

```bash
python run.py campaign --limit 4 --live
```

Các biến quan trọng cho campaign:
- `YOUTUBE_CHANNEL_URLS`, `CUSTOM_NEWS_URLS`
- `PIPELINE_DRY_RUN`, `PIPELINE_AUTO_POST_WORDPRESS`, `PIPELINE_AUTO_POST_FACEBOOK`
- `WP_BASE_URL`, `WP_USERNAME`, `WP_APP_PASSWORD`
- `OPENAI_API_KEY` (nếu muốn tạo ảnh AI)

## Test kết nối API

```bash
python run.py test
```

## Cấu trúc project

```text
fb_page_manager/
├── .env.example           # Biến môi trường mẫu
├── requirements.txt       # Thư viện Python cần cài
├── run.py                 # Entry point: web/scheduler/fetch/test
├── data/
│   └── fb_page_manager.db # SQLite database
└── src/fb_page_manager/
    ├── config.py          # Load config từ .env
    ├── database.py        # SQLite schema + operations
    ├── crawler.py         # RSS + NewsAPI crawler
    ├── source_collector.py # Crawl URL tin + YouTube transcript
    ├── campaign_pipeline.py # Pipeline campaign end-to-end
    ├── wordpress_publisher.py # Đăng bài qua WP REST API
    ├── image_generator.py # Sinh ảnh AI qua OpenAI Image API
    ├── ai_writer.py       # Gemini caption generation
    ├── fb_poster.py       # Facebook Graph API
    ├── scheduler.py       # Lịch đăng với thư viện schedule
    └── web_server.py      # Flask dashboard + REST API
```
