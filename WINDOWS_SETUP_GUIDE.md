# Windows Setup Guide — Web Crawler

This guide covers everything you need to run the web crawler on a **Windows** system. The codebase is fully cross-platform — no code changes are needed.

---

## Prerequisites

| Software        | Version   | Download                                            |
| --------------- | --------- | --------------------------------------------------- |
| Python          | 3.10+     | https://www.python.org/downloads/                   |
| Git (optional)  | Latest    | https://git-scm.com/download/win                    |

> **Important:** During Python installation, check **"Add Python to PATH"** and **"Install py launcher"**.

---

## Step 1: Open Terminal

Use one of:
- **PowerShell** (recommended): Right-click Start → "Terminal" or "Windows PowerShell"
- **Command Prompt**: Press `Win+R`, type `cmd`, hit Enter

Verify Python is installed:

```powershell
python --version
# Should show Python 3.10+ (e.g., Python 3.12.4)
```

If `python` is not found, try `py --version` or reinstall Python with the PATH checkbox enabled.

---

## Step 2: Clone / Copy the Project

If you have Git:
```powershell
git clone <your-repo-url>
cd web_crawler
```

Or copy the `web_crawler` folder to your desired location and navigate to it:
```powershell
cd C:\Users\YourName\Projects\web_crawler
```

---

## Step 3: Create a Virtual Environment

```powershell
python -m venv venv
```

Activate it:
```powershell
# PowerShell
.\venv\Scripts\Activate.ps1

# Command Prompt
venv\Scripts\activate.bat
```

> **PowerShell Execution Policy:** If you get a "running scripts is disabled" error, run:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Then try activating again.

---

## Step 4: Install Dependencies

```powershell
pip install -r requirements.txt
```

All packages in `requirements.txt` have pre-built Windows wheels — no C compiler needed.

---

## Step 5: Install Playwright Browsers

```powershell
playwright install chromium
```

This downloads the Chromium browser binary (~150 MB). Playwright handles this automatically on Windows — no system libraries needed (unlike Linux which requires `libnss3` etc.).

---

## Step 6: Configure Credentials (for SAP / authenticated portals)

Create a `.env` file in the `web_crawler` directory:

```powershell
# Using PowerShell to create .env
@"
# SAP Portal credentials
SAP_USERNAME=your_sap_username
SAP_PASSWORD=your_sap_password
SAP_LOGIN_URL=https://accounts.sap.com/saml2/idp/sso

# Generic credentials (for ServiceNow, etc.)
CRAWLER_USERNAME=your_username
CRAWLER_PASSWORD=your_password
"@ | Out-File -Encoding utf8 .env
```

Or create `.env` manually in any text editor (Notepad, VS Code, etc.) with the same content.

> **Note:** Make sure the `.env` file is saved with **UTF-8 encoding** (not UTF-16). Use `Out-File -Encoding utf8` or VS Code's encoding selector.

---

## Step 7: Run the Crawler

### Basic crawl (public website):
```powershell
python -m crawler "https://docs.python.org/3/library" --pages 50 --output-json output.json
```

### SAP me.sap.com crawl (authenticated):
```powershell
python -m crawler "https://me.sap.com/home" --output-json sap_output.json --pages 25 --login-strategy sap_saml --timeout 60000 --workers 3
```

### Oracle HCM crawl:
```powershell
python -m crawler "https://docs.oracle.com/en/cloud/saas/human-resources/" --pages 100 --output-json oracle.json
```

---

## All CLI Options Reference

| Flag                   | Default | Description                                   |
| ---------------------- | ------- | --------------------------------------------- |
| `url`                  | —       | Starting URL to crawl (required)              |
| `--pages N`            | 300     | Maximum pages to crawl                        |
| `--depth N`            | 5       | Maximum crawl depth from start URL            |
| `--timeout N`          | 20      | Timeout per page in seconds                   |
| `--workers N`          | 6       | Concurrent browser tabs (3 for SAP auto-tune) |
| `--rate N`             | 0.3     | Delay between pages in seconds                |
| `--max-interactions N` | 50      | Max JS interactions per page                  |
| `--output-json FILE`   | —       | JSON output path                              |
| `--output-csv FILE`    | —       | CSV output path                               |
| `--output-docx FILE`   | —       | Word document output path                     |
| `--output-rag-json FILE` | —    | RAG corpus JSON (chunked for embeddings)      |
| `--output-rag-jsonl FILE` | —   | RAG chunks JSONL (one per line)               |
| `--login-strategy X`  | auto    | Auth strategy: `sap_saml`, `servicenow`, etc. |
| `--deny-pattern REGEX` | —      | URL deny pattern (repeatable)                 |
| `--strip-query`        | false   | Strip query strings from URLs                 |
| `--sync`               | false   | Use legacy synchronous crawler                |

---

## Output Format Commands

After crawling, you can export to multiple formats at once:

```powershell
# JSON + CSV + DOCX + RAG all at once
python -m crawler "https://me.sap.com/home" ^
  --pages 25 --login-strategy sap_saml --timeout 60000 --workers 3 ^
  --output-json sap_output.json ^
  --output-csv sap_output.csv ^
  --output-docx sap_output.docx ^
  --output-rag-json sap_rag.json ^
  --output-rag-jsonl sap_rag.jsonl
```

> **Note:** In PowerShell, use backtick `` ` `` for line continuation instead of `^`:
> ```powershell
> python -m crawler "https://me.sap.com/home" `
>   --pages 25 --login-strategy sap_saml --timeout 60000 --workers 3 `
>   --output-json sap_output.json `
>   --output-docx sap_output.docx
> ```

---

## Streamlit Web UI

```powershell
streamlit run app.py
```

Opens at `http://localhost:8501` in your browser. The UI auto-installs Playwright browsers on first run.

---

## SAP Authentication: Fresh Login vs Cached Session

### First-time login (new user / new credentials):
1. Set credentials in `.env`
2. Delete any old session: `del auth_state.json` (if it exists)
3. Run the crawl — it will perform SAML SSO login automatically

### Returning user (reuse session):
Just run the crawl — it auto-loads from `auth_state.json` in the current directory. If the session expired, it auto-re-authenticates.

### Force fresh login (even with existing session):
```powershell
del auth_state.json
python -m crawler "https://me.sap.com/home" --output-json sap_output.json --pages 25 --login-strategy sap_saml --timeout 60000 --workers 3
```

---

## Troubleshooting

### "python" is not recognized
- Reinstall Python from python.org with **"Add to PATH"** checked
- Or use `py -3` instead of `python`

### PowerShell script execution disabled
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Playwright browser install fails
```powershell
# Install with system dependencies (admin PowerShell)
playwright install --with-deps chromium
```

### SSL certificate errors
```powershell
pip install --upgrade certifi
```

### "No module named crawler"
Make sure you are in the `web_crawler` directory (the one containing the `crawler/` folder):
```powershell
cd C:\path\to\web_crawler
python -m crawler --help
```

### SAP login fails
- Verify credentials in `.env` are correct
- Delete `auth_state.json` and retry
- Check if your SAP account requires MFA (not supported — use a service account)

### Slow crawl / timeouts
- SAP pages take 40-60s each due to UI5 framework rendering
- Use `--workers 3` for SAP (auto-tuned, but explicit is fine)
- For faster sites, increase `--workers 6` or higher

### Memory issues with large crawls
- Reduce `--workers` to 2
- Reduce `--pages` to crawl in batches

---

## Differences from macOS

| Feature              | macOS                           | Windows                          |
| -------------------- | ------------------------------- | -------------------------------- |
| Python command       | `python3`                       | `python` or `py`                 |
| Venv activation      | `source venv/bin/activate`      | `.\venv\Scripts\Activate.ps1`    |
| Line continuation    | `\`                             | `` ` `` (PowerShell) or `^` (CMD) |
| File deletion        | `rm file`                       | `del file` or `Remove-Item file` |
| Path separator       | `/`                             | `\` (handled automatically)      |
| Playwright browsers  | `~/.cache/ms-playwright/`       | `%USERPROFILE%\AppData\Local\ms-playwright\` |

**No code changes needed.** The crawler auto-detects your OS for the `sec-ch-ua-platform` header, and all file paths use Python's `pathlib.Path` for cross-platform compatibility.

---

## Quick Start (Copy-Paste)

```powershell
# Full setup from scratch
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium

# Create .env with your credentials
notepad .env

# Run a test crawl
python -m crawler "https://books.toscrape.com" --pages 10 --output-json test.json

# Run SAP crawl
python -m crawler "https://me.sap.com/home" --pages 25 --login-strategy sap_saml --timeout 60000 --workers 3 --output-json sap_output.json
```
