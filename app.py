"""
Web Crawler - Streamlit Frontend
A production-grade web crawling application with a user-friendly interface.
"""

import streamlit as st
import pandas as pd
import json
import time
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
import threading
from io import StringIO, BytesIO

# Install Playwright browsers on first run (for Streamlit Cloud)
@st.cache_resource
def install_playwright_browsers():
    """Install Playwright Chromium browser on first run."""
    try:
        # Check if browsers are already installed
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
                browser.close()
                return True
            except Exception:
                pass
        
        # Try to install browsers
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300
        )
        return result.returncode == 0
    except Exception as e:
        return False

# Try to install Playwright browsers
_playwright_available = install_playwright_browsers()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import crawler components
from crawler.crawler import WebCrawler, CrawlConfig, CrawlResult
from crawler.utils import is_valid_url, URLNormalizer

# Page configuration
st.set_page_config(
    page_title="Web Crawler",
    page_icon="🕷️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS – light / white theme
st.markdown("""
<style>
    /* Force light background */
    .stApp {
        background-color: #FFFFFF;
    }

    .main-header {
        font-size: 2.5rem;
        font-weight: 700;
        color: #1E293B;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1rem;
        color: #64748B;
        margin-bottom: 2rem;
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: #F8F9FB;
    }

    /* Metric cards */
    [data-testid="stMetric"] {
        background-color: #F8F9FB;
        border: 1px solid #E2E8F0;
        border-radius: 10px;
        padding: 0.75rem;
    }
    [data-testid="stMetricValue"] {
        color: #2563EB;
    }

    /* Buttons */
    .stButton > button[kind="primary"] {
        background-color: #2563EB;
        color: #FFFFFF;
        border: none;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #1D4ED8;
    }
    .stButton > button[kind="secondary"] {
        background-color: #FFFFFF;
        color: #1E293B;
        border: 1px solid #CBD5E1;
    }

    /* Input fields */
    .stTextInput > div > div > input {
        background-color: #FFFFFF;
        color: #1E293B;
        border: 1px solid #CBD5E1;
    }

    /* Data frames */
    .stDataFrame {
        border: 1px solid #E2E8F0;
        border-radius: 8px;
    }

    /* Expanders */
    .streamlit-expanderHeader {
        background-color: #F8F9FB;
        color: #1E293B;
        border-radius: 8px;
    }

    /* Download buttons */
    .stDownloadButton > button {
        background-color: #F0F9FF;
        color: #2563EB;
        border: 1px solid #BFDBFE;
    }
    .stDownloadButton > button:hover {
        background-color: #DBEAFE;
    }

    /* Code / log blocks */
    .stCodeBlock, pre {
        background-color: #F1F5F9 !important;
        color: #1E293B !important;
        border: 1px solid #E2E8F0;
        border-radius: 6px;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab"] {
        color: #64748B;
    }
    .stTabs [aria-selected="true"] {
        color: #2563EB;
    }

    /* Progress bar */
    .stProgress > div > div > div {
        background-color: #2563EB;
    }
</style>
""", unsafe_allow_html=True)


def init_session_state():
    """Initialize session state variables."""
    if 'crawl_running' not in st.session_state:
        st.session_state.crawl_running = False
    if 'crawl_result' not in st.session_state:
        st.session_state.crawl_result = None
    if 'crawl_logs' not in st.session_state:
        st.session_state.crawl_logs = []
    if 'pages_crawled' not in st.session_state:
        st.session_state.pages_crawled = 0
    if 'current_url' not in st.session_state:
        st.session_state.current_url = ""
    if 'crawl_stats' not in st.session_state:
        st.session_state.crawl_stats = {}
    if 'crawler_instance' not in st.session_state:
        st.session_state.crawler_instance = None


def add_log(message: str):
    """Add a log message."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.crawl_logs.append(f"[{timestamp}] {message}")
    # Keep only last 100 logs
    if len(st.session_state.crawl_logs) > 100:
        st.session_state.crawl_logs = st.session_state.crawl_logs[-100:]


def progress_callback(pages_crawled: int, current_url: str, stats: dict):
    """Callback for crawl progress updates."""
    st.session_state.pages_crawled = pages_crawled
    st.session_state.current_url = current_url
    st.session_state.crawl_stats = stats


def run_crawler(
    url: str,
    max_depth: int,
    max_pages: int,
    requests_per_second: float,
    respect_robots: bool,
    enable_js: bool,
    strategy: str
) -> CrawlResult:
    """Run the crawler with given parameters."""
    
    config = CrawlConfig(
        max_depth=max_depth,
        max_pages=max_pages,
        requests_per_second=requests_per_second,
        respect_robots=respect_robots,
        enable_js_rendering=enable_js,
        auto_detect_js=enable_js
    )
    
    crawler = WebCrawler(config)
    st.session_state.crawler_instance = crawler
    crawler.set_progress_callback(progress_callback)
    
    try:
        result = crawler.crawl(url, strategy=strategy)
        return result
    finally:
        st.session_state.crawler_instance = None


def export_to_json(result: CrawlResult) -> str:
    """Export result to JSON string."""
    data = {
        'metadata': {
            'total_pages': len(result.pages),
            'crawl_stats': result.stats,
            'config': result.config,
            'scope_info': result.scope_info,
            'errors_count': len(result.errors),
            'exported_at': datetime.now().isoformat()
        },
        'pages': [page.to_dict() for page in result.pages],
        'errors': result.errors
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def export_to_csv(result: CrawlResult) -> str:
    """Export result to CSV string."""
    if not result.pages:
        return ""
    
    rows = [page.to_flat_dict() for page in result.pages]
    df = pd.DataFrame(rows)
    return df.to_csv(index=False)


def export_to_docx(result: CrawlResult) -> bytes:
    """Export result to DOCX bytes for download."""
    import tempfile, os
    crawler = WebCrawler()
    tmp = tempfile.NamedTemporaryFile(suffix='.docx', delete=False)
    tmp.close()
    try:
        crawler.export_docx(result, tmp.name)
        with open(tmp.name, 'rb') as f:
            return f.read()
    finally:
        os.unlink(tmp.name)


def render_sidebar():
    """Render the sidebar with configuration options."""
    st.sidebar.markdown("## ⚙️ Crawler Settings")
    
    # Crawl depth
    max_depth = st.sidebar.slider(
        "Max Crawl Depth",
        min_value=1,
        max_value=10,
        value=3,
        help="How deep to follow links from the starting page"
    )
    
    # Max pages
    max_pages = st.sidebar.number_input(
        "Max Pages to Crawl",
        min_value=1,
        max_value=10000,
        value=30,
        step=10,
        help="Maximum number of pages to crawl"
    )
    
    # Request rate
    requests_per_second = st.sidebar.slider(
        "Requests per Second",
        min_value=0.1,
        max_value=5.0,
        value=1.0,
        step=0.1,
        help="Rate limiting for politeness. Lower = slower but safer"
    )
    
    # Crawl strategy
    strategy = st.sidebar.selectbox(
        "Crawl Strategy",
        options=["bfs", "dfs"],
        format_func=lambda x: "Breadth-First (BFS)" if x == "bfs" else "Depth-First (DFS)",
        help="BFS explores all links at current depth before going deeper. DFS goes deep first."
    )
    
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 🔧 Advanced Options")
    
    # Robots.txt
    respect_robots = st.sidebar.checkbox(
        "Respect robots.txt",
        value=True,
        help="Follow robots.txt crawling rules"
    )
    
    # JavaScript rendering - check if Playwright is available
    js_available = _playwright_available
    enable_js = st.sidebar.checkbox(
        "Enable JavaScript Rendering",
        value=js_available,
        disabled=not js_available,
        help="Use headless browser for JS-heavy pages (requires Playwright)" + 
             ("" if js_available else " - ⚠️ Not available on this deployment")
    )
    if not js_available:
        st.sidebar.caption("⚠️ JS rendering unavailable. Static HTML only.")
    
    # Output format
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 📤 Output Format")
    
    output_json = st.sidebar.checkbox("Export as JSON", value=True)
    output_csv = st.sidebar.checkbox("Export as CSV", value=True)
    output_docx = st.sidebar.checkbox("Export as Word (.docx)", value=False)
    
    # Scope guide
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 📖 How Crawling Scope Works")
    
    with st.sidebar.expander("Understanding URL Scope", expanded=False):
        st.markdown("""
**Root Domain URL:**
```
https://example.com
```
→ Crawls the **entire website**

**Sub-path URL:**
```
https://example.com/blog
```
→ Crawls **only** `/blog/**` pages

---

**Examples:**

If you enter:
`https://oracle.com/java/technologies`

✅ **Will crawl:**
- `/java/technologies`
- `/java/technologies/javase`
- `/java/technologies/downloads`

❌ **Will NOT crawl:**
- `/cloud`
- `/database`
- `/index.html`
        """)
    
    return {
        'max_depth': max_depth,
        'max_pages': max_pages,
        'requests_per_second': requests_per_second,
        'strategy': strategy,
        'respect_robots': respect_robots,
        'enable_js': enable_js,
        'output_json': output_json,
        'output_csv': output_csv,
        'output_docx': output_docx
    }


def render_metrics(stats: dict):
    """Render crawl metrics."""
    cols = st.columns(4)
    
    with cols[0]:
        st.metric(
            "Pages Crawled",
            stats.get('pages_crawled', 0)
        )
    
    with cols[1]:
        st.metric(
            "Failed",
            stats.get('pages_failed', 0)
        )
    
    with cols[2]:
        st.metric(
            "Skipped",
            stats.get('pages_skipped', 0)
        )
    
    with cols[3]:
        elapsed = stats.get('elapsed_time', 0)
        st.metric(
            "Time Elapsed",
            f"{elapsed:.1f}s"
        )


def render_results(result: CrawlResult, output_json: bool, output_csv: bool, output_docx: bool = False):
    """Render crawl results and download buttons."""
    st.markdown("---")
    st.markdown("## 📊 Results")
    
    # Show scope used for this crawl
    if result.scope_info:
        scope_desc = result.scope_info.get('scope_description', 'Unknown')
        if result.scope_info.get('is_root_domain'):
            st.success(f"🌐 **Crawl Scope:** {scope_desc}")
        else:
            st.info(f"📂 **Crawl Scope:** {scope_desc}")
    
    # Summary metrics
    render_metrics(result.stats)
    
    # Pages per second
    pps = result.stats.get('pages_per_second', 0)
    st.info(f"🚀 Average speed: {pps:.2f} pages/second")
    
    # Results table
    if result.pages:
        st.markdown("### 📄 Crawled Pages")
        
        # Create summary dataframe
        df_data = []
        for page in result.pages:
            df_data.append({
                'URL': page.url,
                'Title': page.title[:100] + '...' if len(page.title) > 100 else page.title,
                'Words': page.word_count,
                'Links': len(page.internal_links),
                'Depth': page.crawl_depth,
                'Status': page.status_code
            })
        
        df = pd.DataFrame(df_data)
        st.dataframe(df, width="stretch")
        
        # Download buttons
        st.markdown("### 📥 Download Results")
        
        col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
        
        with col1:
            if output_json:
                json_data = export_to_json(result)
                st.download_button(
                    label="📥 Download JSON",
                    data=json_data,
                    file_name=f"crawl_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                    mime="application/json"
                )
        
        with col2:
            if output_csv:
                csv_data = export_to_csv(result)
                st.download_button(
                    label="📥 Download CSV",
                    data=csv_data,
                    file_name=f"crawl_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv"
                )
        
        with col3:
            if output_docx:
                docx_data = export_to_docx(result)
                st.download_button(
                    label="📥 Download DOCX",
                    data=docx_data,
                    file_name=f"crawl_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
    
    # Errors
    if result.errors:
        with st.expander(f"⚠️ Errors ({len(result.errors)})", expanded=False):
            for error in result.errors[:50]:  # Show first 50 errors
                st.error(f"**{error.get('url', 'Unknown')}**: {error.get('error', 'Unknown error')}")


def render_page_details(result: CrawlResult):
    """Render detailed view of crawled pages."""
    if not result or not result.pages:
        return
    
    st.markdown("---")
    st.markdown("## 🔍 Page Details")
    
    # Page selector
    page_urls = [p.url for p in result.pages]
    selected_url = st.selectbox("Select a page to view details:", page_urls)
    
    if selected_url:
        page = next((p for p in result.pages if p.url == selected_url), None)
        if page:
            col1, col2 = st.columns(2)
            
            with col1:
                st.markdown("**Title:**")
                st.write(page.title or "N/A")
                
                st.markdown("**Meta Description:**")
                st.write(page.meta_description or "N/A")
                
                st.markdown("**Headings:**")
                if page.headings:
                    for level, headings in page.headings.items():
                        st.markdown(f"*{level.upper()}:*")
                        for h in headings[:5]:  # Limit to 5 per level
                            st.write(f"  - {h}")
                else:
                    st.write("No headings found")
            
            with col2:
                st.markdown("**Statistics:**")
                st.write(f"- Word Count: {page.word_count}")
                st.write(f"- Internal Links: {len(page.internal_links)}")
                st.write(f"- External Links: {len(page.external_links)}")
                st.write(f"- Crawl Depth: {page.crawl_depth}")
                st.write(f"- Status Code: {page.status_code}")
                
                st.markdown("**Internal Links (first 10):**")
                for link in page.internal_links[:10]:
                    st.write(f"  - {link}")
            
            with st.expander("📝 Full Text Content", expanded=False):
                st.text(page.text_content[:5000] + "..." if len(page.text_content) > 5000 else page.text_content)


def main():
    """Main application."""
    init_session_state()
    
    # Header
    st.markdown('<p class="main-header">🕷️ Web Crawler</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="sub-header">Production-grade web crawler with JavaScript rendering support</p>',
        unsafe_allow_html=True
    )
    
    # Sidebar configuration
    config = render_sidebar()
    
    # Main content area
    st.markdown("## 🌐 Enter Website URL")
    
    # URL input
    col1, col2 = st.columns([4, 1])
    
    with col1:
        url = st.text_input(
            "Website URL",
            placeholder="https://example.com",
            help="Enter the root URL of the website you want to crawl",
            label_visibility="collapsed"
        )
    
    with col2:
        crawl_button = st.button(
            "🚀 Start Crawl",
            type="primary",
            disabled=st.session_state.crawl_running
        )
    
    # Show scope preview when URL is entered
    if url and is_valid_url(url):
        normalizer = URLNormalizer()
        scope_info = normalizer.get_scope_info(url)
        
        if scope_info['is_root_domain']:
            st.info(f"🌐 **Scope: Entire Domain** — Will crawl all pages under `{scope_info['base_domain']}`")
        else:
            st.warning(
                f"📂 **Scope: Sub-path Only** — Will only crawl pages under `{scope_info['base_domain']}{scope_info['base_path']}/**`\n\n"
                f"✅ Example allowed: `{scope_info['example_allowed']}`\n\n"
                f"❌ Example blocked: `{scope_info['example_blocked']}`"
            )
    
    # Stop button
    if st.session_state.crawl_running:
        if st.button("⏹️ Stop Crawl", type="secondary"):
            if st.session_state.crawler_instance:
                st.session_state.crawler_instance.stop()
                add_log("Stop requested...")
    
    # Validation
    if crawl_button:
        if not url:
            st.error("Please enter a URL to crawl")
        elif not is_valid_url(url):
            st.error("Please enter a valid URL (e.g., https://example.com)")
        else:
            # Start crawling
            st.session_state.crawl_running = True
            st.session_state.crawl_result = None
            st.session_state.crawl_logs = []
            st.session_state.pages_crawled = 0
            st.session_state.current_url = ""
            st.session_state.crawl_stats = {}
            
            add_log(f"Starting crawl of {url}")
            add_log(f"Max depth: {config['max_depth']}, Max pages: {config['max_pages']}")
            
            # Progress placeholder
            progress_placeholder = st.empty()
            status_placeholder = st.empty()
            metrics_placeholder = st.empty()
            
            try:
                with st.spinner("Crawling in progress..."):
                    # Create a progress container
                    progress_bar = progress_placeholder.progress(0)
                    
                    result = run_crawler(
                        url=url,
                        max_depth=config['max_depth'],
                        max_pages=config['max_pages'],
                        requests_per_second=config['requests_per_second'],
                        respect_robots=config['respect_robots'],
                        enable_js=config['enable_js'],
                        strategy=config['strategy']
                    )
                    
                    st.session_state.crawl_result = result
                    add_log(f"Crawl complete! {len(result.pages)} pages crawled")
                    
            except Exception as e:
                st.error(f"Crawl failed: {str(e)}")
                add_log(f"Error: {str(e)}")
                logger.exception("Crawl failed")
            
            finally:
                st.session_state.crawl_running = False
                progress_placeholder.empty()
                status_placeholder.empty()
                st.rerun()
    
    # Display progress during crawl
    if st.session_state.crawl_running:
        st.markdown("### 📈 Progress")
        
        progress = st.session_state.pages_crawled / max(config['max_pages'], 1)
        st.progress(min(progress, 1.0))
        
        st.write(f"**Pages crawled:** {st.session_state.pages_crawled}")
        if st.session_state.current_url:
            st.write(f"**Current URL:** {st.session_state.current_url}")
        
        if st.session_state.crawl_stats:
            render_metrics(st.session_state.crawl_stats)
    
    # Display results
    if st.session_state.crawl_result:
        render_results(
            st.session_state.crawl_result,
            config['output_json'],
            config['output_csv'],
            config['output_docx']
        )
        render_page_details(st.session_state.crawl_result)
    
    # Logs section
    if st.session_state.crawl_logs:
        with st.expander("📋 Crawl Logs", expanded=False):
            log_text = "\n".join(st.session_state.crawl_logs[-50:])
            st.code(log_text, language=None)
    
    # Footer
    st.markdown("---")
    st.markdown(
        """
        <div style='text-align: center; color: #666; font-size: 0.8rem;'>
            <p>🕷️ Production Web Crawler | Built with Streamlit</p>
            <p>Please crawl responsibly and respect website terms of service</p>
        </div>
        """,
        unsafe_allow_html=True
    )


if __name__ == "__main__":
    main()
