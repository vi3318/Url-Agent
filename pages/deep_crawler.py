"""
Deep Documentation Crawler - Streamlit Page
Specialized interface for crawling documentation sites with expandable content.
"""

import streamlit as st
import pandas as pd
import json
import time
import logging
import tempfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Import deep crawler
from crawler.deep_crawler import DeepDocCrawler, DeepCrawlConfig, DeepCrawlResult


def render_deep_crawler_page():
    """Render the deep documentation crawler interface."""
    
    st.markdown("## 🔍 Deep Documentation Crawler")
    st.markdown("""
    This specialized crawler is designed for **hierarchical documentation sites** where content is hidden behind:
    - 📂 Expandable dropdowns and accordions
    - 🌳 Tree navigation with nested items
    - 📑 Tabbed content sections
    
    The crawler will **click to expand** all collapsible elements and **follow revealed links**.
    """)
    
    # URL Input
    url = st.text_input(
        "📎 Documentation URL",
        placeholder="https://docs.oracle.com/en/cloud/saas/human-resources/oedmh/index.html",
        help="Enter the starting URL of the documentation site"
    )
    
    # Sidebar settings
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 🔍 Deep Crawler Settings")
    
    max_pages = st.sidebar.number_input(
        "Max Pages",
        min_value=1,
        max_value=1000,
        value=50,
        step=10,
        help="Maximum number of pages to crawl"
    )
    
    max_depth = st.sidebar.number_input(
        "Max Depth",
        min_value=1,
        max_value=20,
        value=5,
        help="Maximum depth to follow links"
    )
    
    delay = st.sidebar.slider(
        "Delay Between Pages (s)",
        min_value=0.5,
        max_value=5.0,
        value=1.0,
        step=0.5,
        help="Time to wait between page loads"
    )
    
    headless = st.sidebar.checkbox(
        "Headless Mode",
        value=True,
        help="Run browser in headless mode (recommended)"
    )
    
    # Custom selectors (advanced)
    with st.sidebar.expander("🔧 Advanced: Custom Selectors"):
        st.markdown("**Expandable Element Selectors**")
        st.caption("CSS selectors for elements to click and expand")
        custom_expandables = st.text_area(
            "Expandable Selectors (one per line)",
            value="",
            height=100,
            help="Leave empty to use defaults. Add custom selectors for site-specific dropdowns."
        )
        
        st.markdown("**Content Selectors**")
        custom_content = st.text_area(
            "Content Selectors (one per line)",
            value="",
            height=80,
            help="CSS selectors for main content areas"
        )
    
    # Export options
    st.sidebar.markdown("---")
    st.sidebar.markdown("## 📤 Export Options")
    export_json = st.sidebar.checkbox("Export as JSON", value=True)
    export_csv = st.sidebar.checkbox("Export as CSV", value=True)
    
    # Progress placeholder
    progress_container = st.empty()
    status_container = st.empty()
    
    # Crawl button
    col1, col2, col3 = st.columns([1, 1, 2])
    
    with col1:
        start_crawl = st.button("🚀 Start Deep Crawl", type="primary", use_container_width=True)
    
    with col2:
        if st.button("🛑 Stop", use_container_width=True):
            if 'deep_crawler_instance' in st.session_state and st.session_state.deep_crawler_instance:
                st.session_state.deep_crawler_instance.stop()
                st.warning("Stop requested...")
    
    # Initialize session state
    if 'deep_crawl_result' not in st.session_state:
        st.session_state.deep_crawl_result = None
    if 'deep_crawler_instance' not in st.session_state:
        st.session_state.deep_crawler_instance = None
    
    if start_crawl and url:
        # Validate URL
        if not url.startswith(('http://', 'https://')):
            st.error("Please enter a valid URL starting with http:// or https://")
            return
        
        # Build config
        config = DeepCrawlConfig(
            max_pages=max_pages,
            max_depth=max_depth,
            delay_between_pages=delay,
            headless=headless,
        )
        
        # Add custom selectors if provided
        if custom_expandables.strip():
            config.expandable_selectors.extend(custom_expandables.strip().split('\n'))
        if custom_content.strip():
            config.content_selectors = custom_content.strip().split('\n') + config.content_selectors
        
        # Create crawler
        crawler = DeepDocCrawler(config)
        st.session_state.deep_crawler_instance = crawler
        
        # Progress callback
        progress_bar = progress_container.progress(0, text="Starting...")
        
        def update_progress(pages_crawled, current_url, stats):
            progress = min(pages_crawled / max_pages, 1.0)
            progress_bar.progress(
                progress,
                text=f"Crawled {pages_crawled}/{max_pages} pages | Expanded {stats.get('expandables_clicked', 0)} elements"
            )
            status_container.caption(f"Current: {current_url[:80]}...")
        
        crawler.set_progress_callback(update_progress)
        
        # Start crawling
        with st.spinner("Deep crawling in progress... This may take a while."):
            try:
                result = crawler.crawl(url)
                st.session_state.deep_crawl_result = result
                st.session_state.deep_crawler_instance = None
                
                progress_bar.progress(1.0, text="✅ Crawl complete!")
                status_container.empty()
                
                st.success(f"Crawled **{len(result.pages)}** pages in **{result.stats.get('elapsed_time', 0):.1f}s**")
                
            except Exception as e:
                st.error(f"Crawl failed: {str(e)}")
                logger.exception("Deep crawl error")
    
    # Display results
    if st.session_state.deep_crawl_result:
        result = st.session_state.deep_crawl_result
        
        st.markdown("---")
        st.markdown("## 📊 Results")
        
        # Stats
        cols = st.columns(5)
        with cols[0]:
            st.metric("Pages Crawled", result.stats.get('pages_crawled', 0))
        with cols[1]:
            st.metric("Elements Expanded", result.stats.get('expandables_clicked', 0))
        with cols[2]:
            st.metric("Links Discovered", result.stats.get('links_discovered', 0))
        with cols[3]:
            st.metric("Errors", result.stats.get('pages_failed', 0))
        with cols[4]:
            st.metric("Time (s)", f"{result.stats.get('elapsed_time', 0):.1f}")
        
        # Pages table
        if result.pages:
            st.markdown("### 📄 Crawled Pages")
            
            df_data = []
            for page in result.pages:
                df_data.append({
                    'Title': page.title[:60] + '...' if len(page.title) > 60 else page.title,
                    'Section Path': ' > '.join(page.section_path[-3:]) if page.section_path else '-',
                    'Words': page.word_count,
                    'Tables': len(page.tables),
                    'Depth': page.depth,
                    'URL': page.url,
                })
            
            df = pd.DataFrame(df_data)
            st.dataframe(df, use_container_width=True, hide_index=True)
            
            # Download buttons
            st.markdown("### 📥 Download Results")
            
            col1, col2, col3 = st.columns([1, 1, 2])
            
            with col1:
                if export_json:
                    json_data = json.dumps({
                        'stats': result.stats,
                        'pages': [p.to_dict() for p in result.pages],
                        'errors': result.errors,
                    }, indent=2, ensure_ascii=False)
                    
                    st.download_button(
                        "📥 Download JSON",
                        data=json_data,
                        file_name=f"deep_crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                        mime="application/json"
                    )
            
            with col2:
                if export_csv:
                    rows = [p.to_flat_dict() for p in result.pages]
                    csv_df = pd.DataFrame(rows)
                    csv_data = csv_df.to_csv(index=False)
                    
                    st.download_button(
                        "📥 Download CSV",
                        data=csv_data,
                        file_name=f"deep_crawl_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv"
                    )
        
        # Page details
        if result.pages:
            st.markdown("---")
            st.markdown("### 🔎 Page Details")
            
            page_options = [f"{p.title[:50]}... ({p.url.split('/')[-1]})" for p in result.pages]
            selected_idx = st.selectbox(
                "Select a page to view details:",
                range(len(page_options)),
                format_func=lambda x: page_options[x]
            )
            
            if selected_idx is not None:
                page = result.pages[selected_idx]
                
                st.markdown(f"**URL:** {page.url}")
                st.markdown(f"**Section Path:** {' > '.join(page.section_path)}")
                st.markdown(f"**Words:** {page.word_count}")
                
                # Headings
                if page.headings:
                    with st.expander("📑 Headings", expanded=False):
                        for level, heads in page.headings.items():
                            if heads:
                                st.markdown(f"**{level.upper()}:** {', '.join(heads[:5])}")
                
                # Tables
                if page.tables:
                    with st.expander(f"📊 Tables ({len(page.tables)})", expanded=False):
                        for i, table in enumerate(page.tables[:3]):
                            st.markdown(f"**Table {i+1}**")
                            if table.get('headers'):
                                st.write(f"Headers: {table['headers']}")
                            if table.get('rows'):
                                st.write(f"Rows: {len(table['rows'])}")
                
                # Text content
                with st.expander("📝 Text Content", expanded=False):
                    st.text_area(
                        "Content",
                        value=page.text_content[:5000],
                        height=300,
                        disabled=True
                    )
        
        # Errors
        if result.errors:
            with st.expander(f"⚠️ Errors ({len(result.errors)})", expanded=False):
                for error in result.errors[:20]:
                    st.error(f"**{error.get('url', 'Unknown')}**: {error.get('error', 'Unknown error')}")


# Main entry point when run as page
if __name__ == "__main__":
    st.set_page_config(
        page_title="Deep Documentation Crawler",
        page_icon="🔍",
        layout="wide"
    )
    render_deep_crawler_page()
