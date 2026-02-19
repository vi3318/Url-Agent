#!/usr/bin/env python3
"""Analyze Oracle HCM crawl output for content quality issues."""
import json

data = json.load(open('docs_oracle_com_en_cloud_saas_human-resources_.json'))
pages = data['pages']
print(f'Total pages: {len(pages)}')
print(f'Total words: {sum(p.get("word_count", 0) for p in pages)}')
print()

# Count issues
share_count = 0
search_count = 0
footer_count = 0
feedback_count = 0
dup_toc_count = 0

for p in pages:
    tc = p.get('text_content', '')
    wc = p.get('word_count', 0)
    url = p.get('url', '')

    has_share = 'Share on LinkedIn' in tc or 'Share on X' in tc
    has_search_junk = 'Search Unavailable' in tc or 'No matching results' in tc
    has_footer = '© Oracle' in tc or 'About Oracle' in tc or 'Terms of Use' in tc
    has_feedback = 'Was this page helpful' in tc
    
    # Check for duplicated TOC (same headings appear twice)
    lines = [l.strip() for l in tc.split('\n') if l.strip()]
    numbered_lines = [l for l in lines if l and l[0].isdigit()]
    unique_numbered = set(numbered_lines)
    has_dup_toc = len(numbered_lines) > len(unique_numbered) and len(unique_numbered) > 3

    issues = []
    if has_share:
        issues.append('SHARE')
        share_count += 1
    if has_search_junk:
        issues.append('SEARCH')
        search_count += 1
    if has_footer:
        issues.append('FOOTER')
        footer_count += 1
    if has_feedback:
        issues.append('FEEDBACK')
        feedback_count += 1
    if has_dup_toc:
        issues.append('DUP-TOC')
        dup_toc_count += 1

    if issues:
        path = url.replace('https://docs.oracle.com/en/cloud/saas/human-resources/', '')
        print(f'  {wc:>5}w  [{" ".join(issues):30s}]  {path[:60]}')

print()
print(f'=== SUMMARY ===')
print(f'Pages with Share button text: {share_count}/{len(pages)}')
print(f'Pages with Search UI junk:    {search_count}/{len(pages)}')
print(f'Pages with Footer text:       {footer_count}/{len(pages)}')
print(f'Pages with Feedback widget:   {feedback_count}/{len(pages)}')
print(f'Pages with Duplicated TOC:    {dup_toc_count}/{len(pages)}')

# Show a few examples of bad content
print()
print('=== WORST EXAMPLES ===')
for p in pages:
    tc = p.get('text_content', '')
    if 'Share on LinkedIn' in tc:
        url = p.get('url', '')
        path = url.replace('https://docs.oracle.com/en/cloud/saas/human-resources/', '')
        print(f'\n--- {path} ({p["word_count"]}w) ---')
        # Show first 500 chars
        print(tc[:500])
        print('...')
        break

# Show pages that are only TOC
print()
print('=== TOC-ONLY PAGES (content is just chapter headings) ===')
toc_pages = []
for p in pages:
    tc = p.get('text_content', '')
    lines = [l.strip() for l in tc.split('\n') if l.strip()]
    # If >80% of lines start with a number or are short headings
    if len(lines) > 5:
        heading_like = sum(1 for l in lines if l[0].isdigit() or l.startswith('Title') or l.startswith('Get Help'))
        if heading_like / len(lines) > 0.7:
            toc_pages.append(p)

print(f'Pages that look TOC-only: {len(toc_pages)}')
for p in toc_pages[:5]:
    url = p.get('url', '')
    path = url.replace('https://docs.oracle.com/en/cloud/saas/human-resources/', '')
    print(f'  {p["word_count"]:>5}w  {path[:70]}')
