# fb_page_manager

Python project to automate content workflow for a Facebook Page:
1. Crawl content from RSS and NewsAPI
2. Rewrite caption with Claude API
3. Schedule posts
4. Publish via Facebook Graph API
5. Track status in SQLite

## Project structure

```text
fb_page_manager/
├─ .env.example
├─ .gitignore
├─ requirements.txt
├─ run.py
└─ src/
   └─ fb_page_manager/
      ├─ __init__.py
      ├─ ai_writer.py
      ├─ config.py
      ├─ crawler.py
      ├─ database.py
      ├─ fb_poster.py
      ├─ main.py
      └─ scheduler.py
```

## Quick start

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` with real API credentials, then run:

```bash
python run.py --once
```

Or run continuous scheduler:

```bash
python run.py
```

