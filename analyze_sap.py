import json

with open('sap_test_new.json') as f:
    data = json.load(f)

pages = data.get('pages', [])
errors = data.get('errors', [])

total_words = 0
empty_pages = 0
junk_pages = 0

print('=== CRAWLED PAGES ===')
for p in pages:
    url = p.get('url', '')
    wc = p.get('word_count', 0)
    title = p.get('title', 'No title')
    text = p.get('text_content', '')
    total_words += wc
    if wc == 0:
        empty_pages += 1

    has_junk = any(j in text for j in [
        'Restricted Card Content',
        'not authorized to see',
        'Header Title (Not Shown)',
    ])
    if has_junk:
        junk_pages += 1

    junk_flag = ' [JUNK]' if has_junk else ''
    preview = text[:120].replace('\n', ' | ')
    print(f'  {url}')
    print(f'    title={title}, words={wc}{junk_flag}')
    print(f'    preview: {preview}')
    print()

print(f'=== SUMMARY ===')
print(f'Total pages: {len(pages)}')
print(f'Total words: {total_words}')
print(f'Avg words/page: {total_words // max(len(pages), 1)}')
print(f'Empty pages: {empty_pages}')
print(f'Junk pages: {junk_pages}')
print(f'Errors: {len(errors)}')

if errors:
    print()
    print('=== ERRORS ===')
    seen = set()
    for e in errors:
        url = e.get('url', '')
        err = e.get('error', '')
        key = f'{url}:{err}'
        if key not in seen:
            seen.add(key)
            print(f'  {url}: {err}')
