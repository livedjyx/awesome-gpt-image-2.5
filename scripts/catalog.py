"""Validate prompt records and render the Markdown catalog. Python 3.11+."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MODES = {'G': '直接生图', 'R': '参考图引导生成', 'E': '编辑现有图片'}
STATES = {'planned': '规划中', 'draft': '草稿', 'generated': '已配图', 'verified': '已验证'}
PHASES = {'pilot': '试制', 'first100': '首批补充', 'expansion': '扩展'}
GALLERY_PAGE_SIZE = 20


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def schema_check(root, name, value):
    schema = read_json(root / 'schemas' / f'{name}.schema.json')
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value))
    require(not errors, f'{name}: ' + '; '.join(f"{'/'.join(map(str, e.path))}: {e.message}" for e in errors[:5]))


def asset_path(base, relative):
    path = (base / relative).resolve()
    require(path.is_relative_to(base.resolve()), f'File outside entry directory: {relative}')
    require(path.is_file(), f'Missing file: {relative}')
    return path


def image_check(base, record):
    path = asset_path(base, record['path'])
    require(digest(path) == record['sha256'], f'Image hash mismatch: {record["path"]}')
    with Image.open(path) as picture:
        formats = {'.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG', '.webp': 'WEBP'}
        require(picture.format == formats.get(path.suffix), f'Image format mismatch: {record["path"]}')
        picture.verify()


def check_parameters(value):
    sensitive = {'api_key', 'apikey', 'authorization', 'access_token', 'refresh_token', 'token', 'password', 'secret', 'cookie'}
    if isinstance(value, dict):
        for key, item in value.items():
            require(key.lower().replace('-', '_') not in sensitive, f'Sensitive generation parameter: {key}')
            check_parameters(item)
    elif isinstance(value, list):
        for item in value:
            check_parameters(item)
    elif isinstance(value, str):
        require(not re.search(r'\bsk-[A-Za-z0-9_-]{20,}|\bBearer\s+\S+', value), 'Credential-like parameter value')
        require(not re.search(r'[A-Za-z]:[\\/]|/Users/|/home/', value), 'Local filesystem path in generation parameters')


def validate_entry(root, item, category, base):
    require(base.resolve().is_relative_to(root.resolve()), 'Entry directory outside repository')
    record = read_json(asset_path(base, 'entry.json'))
    schema_check(root, 'entry', record)
    require(record['id'] == item['id'], f'Entry ID mismatch: {base.name}')
    expected = root / 'prompts' / category['slug'] / item['id']
    require(base == expected, f'Wrong category or entry directory: {base}')
    prompt = asset_path(base, record['prompt_file'])
    require(b'\r' not in prompt.read_bytes() and not prompt.read_bytes().startswith(b'\xef\xbb\xbf'), 'Prompt must use UTF-8 without BOM and LF line endings so Git preserves its hash')
    prompt_text = prompt.read_text(encoding='utf-8').strip()
    require(bool(prompt_text), f'Empty prompt: {item["id"]}')
    paths = [r['path'] for r in record['inputs']]
    require(len(paths) <= item['input_count'], f'Too many input images: {item["id"]}')
    require(len(set(paths)) == len(paths), f'Duplicate input paths: {item["id"]}')
    for index, image in enumerate(record['inputs'], 1):
        require(Path(image['path']).stem == f'input-{index:02d}', f'Input order/name mismatch: {item["id"]}')
        image_check(base, image)
    if record['result'] is not None:
        image_check(base, record['result'])
    generation = record['generation']
    if generation:
        check_parameters(generation['parameters'])
        require(digest(prompt) == generation['prompt_sha256'], f'Prompt hash mismatch: {item["id"]}')
        require(not generation['model_confirmed'] or generation['model'] is not None, 'Confirmed model must have an identifier')
    if record['status'] != 'draft':
        require(len(record['inputs']) == item['input_count'], f'Input count mismatch: {item["id"]}')
        template = (root / 'templates/prompt.md').read_text(encoding='utf-8').strip()
        require(prompt_text != template, f'Unreplaced prompt template: {item["id"]}')
        require(record['result']['sha256'] not in {image['sha256'] for image in record['inputs']}, 'Result must not be a copy of an input image')
    if record['review'] and generation:
        require(datetime.fromisoformat(record['review']['reviewed_at'].replace('Z', '+00:00')) >= datetime.fromisoformat(generation['generated_at'].replace('Z', '+00:00')), 'Review predates generation')
    if record['status'] == 'verified':
        require(bool(re.fullmatch(r'gpt-image-2\.5(?:[-.][a-zA-Z0-9.-]+)?', generation['model'])), 'Verified gallery requires a confirmed Image 2.5 model identifier')
        review = record['review']
        checks = review['checks']
        for key in ['prompt_alignment', 'style_fidelity', 'readability']:
            require(checks[key] == 'pass', f'Review must pass {key}: {item["id"]}')
        require(checks['text_and_count'] != 'fail', f'Text/count check failed: {item["id"]}')
        require(checks['reference_fidelity'] == ('na' if item['mode'] == 'G' else 'pass'), f'Reference review mismatch: {item["id"]}')
        require(checks['edit_scope'] == ('pass' if item['mode'] == 'E' else 'na'), f'Edit-scope review mismatch: {item["id"]}')
    return record


def load_catalog(root):
    categories = read_json(root / 'data/categories.json')
    items = read_json(root / 'data/catalog.json')
    schema_check(root, 'categories', categories)
    schema_check(root, 'catalog', items)
    by_category = {c['id']: c for c in categories}
    require(len(by_category) == len(categories), 'Duplicate category IDs')
    require(len({c['slug'] for c in categories}) == len(categories), 'Duplicate category paths')
    require(len({i['id'] for i in items}) == len(items), 'Duplicate scene IDs')
    require(len({i['title'] for i in items}) == len(items), 'Duplicate scene titles')
    by_id = {i['id']: i for i in items}
    for item in items:
        require(item['category'] in by_category, f'Unknown category: {item["id"]}')
        require((item['mode'] == 'G') == (item['input_count'] == 0), f'Mode/input mismatch: {item["id"]}')
    entries = {}
    prompts = root / 'prompts'
    if prompts.exists():
        allowed = {c['slug'] for c in categories}
        for category_dir in prompts.iterdir():
            require(category_dir.is_dir() and category_dir.name in allowed, f'Unknown category path: {category_dir.name}')
            for base in category_dir.iterdir():
                if base.name == 'README.md' and base.is_file():
                    continue
                require(base.is_dir() and base.name in by_id, f'Unregistered entry path: {base}')
                require((base / 'entry.json').is_file(), f'Entry metadata missing: {base.name}')
                item = by_id[base.name]
                require(item['id'] not in entries, f'Duplicate entry directory: {item["id"]}')
                entries[item['id']] = validate_entry(root, item, by_category[item['category']], base)
    return categories, items, entries


def cell(value):
    return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '&#124;').replace('\n', ' ')


def fenced(text, language='text'):
    longest = max((len(x) for x in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}{language}\n{text.rstrip()}\n{fence}'


def review_passes(item, entry):
    if not entry or not entry['result'] or not entry['review'] or entry['status'] == 'draft':
        return False
    checks = entry['review']['checks']
    return (all(checks[k] == 'pass' for k in ['prompt_alignment', 'style_fidelity', 'readability'])
            and checks['text_and_count'] != 'fail'
            and checks['reference_fidelity'] == ('na' if item['mode'] == 'G' else 'pass')
            and checks['edit_scope'] == ('pass' if item['mode'] == 'E' else 'na'))


def render(root, categories, items, entries):
    by_category = {c['id']: c for c in categories}
    status = lambda item: entries.get(item['id'], {}).get('status', 'planned')
    counts = Counter(status(item) for item in items)
    showcase = [item for item in items if review_passes(item, entries.get(item['id']))]
    homepage = ['# awesome-gpt-image-2.5', '', '面向 GPT Image 2.5 的独立创作提示词与配图，按用途、风格和输入方式整理。', '',
        '[使用方法](docs/usage.md) · [试制清单](docs/pilot.md) · [制作安排](docs/roadmap.md) · [贡献内容](CONTRIBUTING.md)', '',
        '## 内容进度', '', '| 图库案例 | 已配图 | 草稿 | 待制作选题 |', '|---:|---:|---:|---:|',
        f"| {len(showcase)} | {counts['generated'] + counts['verified']} | {counts['draft']} | {counts['planned']} |", '',
        f'已规划 {len(categories)} 个分类、{len(items)} 个场景。每条正式发布内容提供可复制的完整提示词、对应结果图和实际生成记录；参考图任务另附必要输入。', '',
        '## 分类目录', '', '| 分类 | 规划总数 | 图库案例 |', '|---|---:|---:|']
    output = {}
    for category in categories:
        subset = [i for i in items if i['category'] == category['id']]
        ready = [i for i in subset if review_passes(i, entries.get(i['id']))]
        homepage.append(f"| [{cell(category['name'])}](prompts/{category['slug']}/README.md) | {len(subset)} | {len(ready)} |")
        lines = [f"# {category['name']}", '', '[返回首页](../../README.md) · [使用方法](../../docs/usage.md)', '',
            f'规划 {len(subset)} 个选题，图库案例 {len(ready)} 条。点击已制作的条目可查看完整提示词与图片。', '',
            '| 编号 | 场景 | 类型 | 风格 | 状态 | 批次 |', '|---|---|---|---|---|---|']
        for item in subset:
            title = cell(item['title'])
            if item['id'] in entries:
                title = f"[{title}]({item['id']}/README.md)"
            lines.append(f"| {item['id']} | {title} | {MODES[item['mode']]} | {cell(item['style'])} | {STATES[status(item)]} | {PHASES[item['phase']]} |")
        if ready:
            lines += ['', '## 配图预览', '']
            for item in ready:
                entry = entries[item['id']]
                lines += [f"### {item['title']}", '', f"[查看提示词]({item['id']}/README.md)", '',
                    f"![{cell(item['title'])}：实际生成结果]({item['id']}/{entry['result']['path']})", '']
        output[f"prompts/{category['slug']}/README.md"] = '\n'.join(lines) + '\n'
    homepage += ['', '## 使用方式', '', '| 类型 | 输入方式 |', '|---|---|',
        '| G：直接生图 | 文字；部分条目需给定文案或数据 |', '| R：参考图引导生成 | 参考图片与文字 |', '| E：编辑现有图片 | 待编辑图片、必要素材与修改要求 |', '',
        '## 图库', '']
    if showcase:
        homepage += ['[浏览全部配图](docs/gallery.md) · [查看试制清单](docs/pilot.md)。机器可读索引见 [gallery.json](data/gallery.json)。', '']
        featured_by_category = {}
        for item in showcase:
            featured_by_category.setdefault(item['category'], item)
        featured = list(featured_by_category.values())[:4]
        for item in featured:
            base_rel = f"prompts/{by_category[item['category']]['slug']}/{item['id']}"
            preview = entries[item['id']]['result']['path']
            homepage += [f"### [{item['title']}]({base_rel}/README.md)", '',
                         f'<a href="{base_rel}/README.md"><img src="{base_rel}/{preview}" alt="{cell(item["title"])}" width="480"></a>', '']
    else:
        homepage += ['首批提示词与配图准备中。']
    homepage += ['', '## 许可', '', '提示词、文档、目录数据及可授权配图采用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)；脚本与数据规范采用 [MIT](LICENSES/MIT.txt)。复用时请按 [许可说明](docs/licensing.md) 署名。', '',
        '## 维护', '', '条目规范和图片命名见 [编写规范](docs/entry-format.md)，新增内容可使用 [条目模板](templates/README.md)。', '']
    output['README.md'] = '\n'.join(homepage)
    gallery = []
    for item in items:
        entry = entries.get(item['id'])
        if entry is None:
            continue
        base_rel = f"prompts/{by_category[item['category']]['slug']}/{item['id']}"
        base = root / base_rel
        text = (base / entry['prompt_file']).read_text(encoding='utf-8')
        lines = [f"# {item['title']}", '', '[返回分类](../README.md)', '', f"状态：{STATES[entry['status']]}。类型：{MODES[item['mode']]}。风格：{item['style']}。", '',
            item['scene'], '', f"核心机制：{item['mechanism']}", '', '## 对应结果', '']
        if entry['result']:
            lines += [f"![{cell(item['title'])}：对应生成结果]({entry['result']['path']})", '']
        else:
            lines += ['尚未生成结果图。', '']
        if entry['inputs']:
            lines += ['## 输入图片', '']
            for number, image in enumerate(entry['inputs'], 1):
                lines += [f"输入 {number}：{image['purpose']}", '', f"![输入 {number}]({image['path']})", '']
        lines += ['## 提示词', '', fenced(text), '']
        lines += ['## 检查', '', item['acceptance'], '']
        if entry['review']:
            lines += [entry['review']['notes'], '']
        else:
            lines += ['尚未完成审核。', '']
        lines += ['[生成与检查记录](entry.json) · [提示词原文](prompt.md)', '', '许可：[CC BY 4.0](../../../docs/licensing.md)。', '']
        output[f'{base_rel}/README.md'] = '\n'.join(lines)
        if review_passes(item, entry):
            gallery.append({'id': item['id'], 'title': item['title'], 'category': item['category'], 'mode': item['mode'],
                'style': item['style'], 'page': f'{base_rel}/README.md', 'preview': f"{base_rel}/{entry['result']['path']}",
                'status': entry['status'], 'model': entry['generation']['model']})
    output['data/gallery.json'] = json.dumps(gallery, ensure_ascii=False, indent=2) + '\n'
    output.update(render_gallery(gallery))
    pilot = ['# 试制选题', '', '20 个试制选题覆盖全部分类，另外检查中英排版和画面扩展。制作状态自动同步，试制条目计入首批 100 条。', '',
        '| 编号 | 场景 | 类型 | 输入图数量 | 状态 |', '|---|---|---|---:|---|']
    for item in items:
        if item['phase'] == 'pilot':
            target = f"../prompts/{by_category[item['category']]['slug']}/README.md"
            if item['id'] in entries:
                target = f"../prompts/{by_category[item['category']]['slug']}/{item['id']}/README.md"
            pilot.append(f"| {item['id']} | [{cell(item['title'])}]({target}) | {MODES[item['mode']]} | {item['input_count']} | {STATES[status(item)]} |")
    output['docs/pilot.md'] = '\n'.join(pilot) + '\n'
    return {path: content.rstrip() + '\n' for path, content in output.items()}


def render_gallery(gallery):
    count = max(1, (len(gallery) + GALLERY_PAGE_SIZE - 1) // GALLERY_PAGE_SIZE)
    filenames = ['gallery.md'] + [f'gallery-{n}.md' for n in range(2, count + 1)]
    pages = {}
    for page, filename in enumerate(filenames, 1):
        navigation = ' · '.join(f'第 {n} 页' if n == page else f'[第 {n} 页]({name})'
                                for n, name in enumerate(filenames, 1))
        start = (page - 1) * GALLERY_PAGE_SIZE
        subset = gallery[start:start + GALLERY_PAGE_SIZE]
        lines = [f'# 配图浏览 · 第 {page} 页', '', '[返回首页](../README.md) · [试制清单](pilot.md)', '',
                 f'共 {len(gallery)} 个案例，每页最多 {GALLERY_PAGE_SIZE} 个。点击标题查看完整提示词、输入素材和对应结果。', '', navigation, '']
        for item in subset:
            lines += [f"## [{item['id']} · {item['title']}](../{item['page']})", '',
                      f'<a href="../{item["page"]}"><img src="../{item["preview"]}" alt="{cell(item["title"])}" width="480"></a>', '']
        lines += [navigation, '']
        pages[f'docs/{filename}'] = '\n'.join(lines)
    return pages


def build_or_check(root, action):
    categories, items, entries = load_catalog(root)
    pages = render(root, categories, items, entries)
    stale = []
    for path in (root / 'docs').glob('gallery-*.md'):
        relative = path.relative_to(root).as_posix()
        if re.fullmatch(r'gallery-[2-9][0-9]*\.md|gallery-1[0-9]+\.md', path.name) and relative not in pages:
            if action == 'build':
                require(path.resolve().is_relative_to((root / 'docs').resolve()), 'Gallery output outside docs')
                path.unlink()
            else:
                stale.append(relative)
    for relative, content in pages.items():
        path = root / relative
        require(path.resolve().is_relative_to(root.resolve()), f'Output outside repository: {relative}')
        if action == 'build':
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        elif not path.is_file() or path.read_text(encoding='utf-8') != content:
            stale.append(relative)
    require(not stale, 'Generated files are stale; run build: ' + ', '.join(stale))
    return len(categories), len(items), sum(e['status'] == 'verified' for e in entries.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'check'])
    args = parser.parse_args()
    try:
        categories, scenes, published = build_or_check(ROOT, args.action)
    except (ValueError, OSError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)
    print(f'OK: {categories} categories; {scenes} planned scenes; {published} verified entries.')


if __name__ == '__main__':
    main()
