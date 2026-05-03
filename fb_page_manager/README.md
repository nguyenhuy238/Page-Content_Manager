# FB Page Manager

Dashboard và scheduler tự động cho Facebook Page:
- Thu thập bài từ RSS/NewsAPI
- Tạo caption bằng Claude
- Đưa vào hàng đợi và đăng theo lịch
- Theo dõi thống kê trong SQLite

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
- `CLAUDE_API_KEY`
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
    ├── ai_writer.py       # Claude caption generation
    ├── fb_poster.py       # Facebook Graph API
    ├── scheduler.py       # Lịch đăng với thư viện schedule
    └── web_server.py      # Flask dashboard + REST API
```
