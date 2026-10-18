from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KEEP_HTML_COMMENT = re.compile(r"<!--/?twsix-")
HTML_COMMENT = re.compile(r"<!--(?!\[if|<!\[endif).*?-->", re.S)
BLOCK = re.compile(r"(<(script|style)\b[^>]*>)(.*?)(</\2\s*>)", re.S | re.I)
TYPE_ATTR = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.I)
NOINDEX = '<meta name="robots" content="noindex,nofollow">'
HEAD = re.compile(r"<head(?:\s[^>]*)?>", re.I)

_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KW = ("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else")


def strip_js(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    last = ""
    word = ""
    tmpl_depth: list[int] = []
    brace = 0
    while i < n:
        c = src[i]
        nx = src[i + 1] if i + 1 < n else ""
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "\n":
                    break
                j += 1
            out.append(src[i:j + 1]); i = j + 1; last = c; word = ""
            continue
        if c == "`" or (c == "}" and tmpl_depth and tmpl_depth[-1] == brace):
            if c == "}":
                tmpl_depth.pop()
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2; continue
                if src[j] == "`":
                    break
                if src[j] == "$" and j + 1 < n and src[j + 1] == "{":
                    tmpl_depth.append(brace)
                    j += 1
                    break
                j += 1
            out.append(src[i:j + 1]); i = j + 1; last = "`"; word = ""
            continue
        if c == "/" and nx == "/":
            j = src.find("\n", i)
            if j < 0:
                j = n
            i = j
            continue
        if c == "/" and nx == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                return src
            seg = src[i:j + 2]
            out.append("\n" if "\n" in seg else " ")
            i = j + 2
            continue
        if c == "/" and (last == "" or last in _REGEX_PREV or word in _REGEX_KW):
            j = i + 1; cls = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2; continue
                if ch == "\n":
                    break
                if ch == "[":
                    cls = True
                elif ch == "]":
                    cls = False
                elif ch == "/" and not cls:
                    break
                j += 1
            j += 1
            while j < n and (src[j].isalnum()):
                j += 1
            out.append(src[i:j]); i = j; last = "a"; word = ""
            continue
        if c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        out.append(c)
        if not c.isspace():
            if c.isalnum() or c in "_$":
                word = word + c if last and (last.isalnum() or last in "_$") else c
            else:
                word = ""
            last = c
        i += 1
    text = "".join(out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def strip_css(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                j += 1
            out.append(src[i:j + 1]); i = j + 1; continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                return src
            i = j + 2; continue
        out.append(c); i += 1
    return re.sub(r"\n\s*\n+", "\n", "".join(out))


VERIFY_JS = r"""
const fs=require('fs'),vm=require('vm');let ts=null;try{ts=require('typescript')}catch(e){}
const t=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));const r={};
const P=ts?ts.createPrinter({removeComments:true}):null;
function norm(c,m){const sf=ts.createSourceFile(m?'x.mjs':'x.js',c,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
 if(sf.parseDiagnostics&&sf.parseDiagnostics.length)throw new Error('parse');return P.printFile(sf)}
for(const k in t){const [orig,neu,m]=t[k];try{
  if(ts){let a;try{a=norm(orig,m)}catch(e){a=null}r[k]=a!==null&&a===norm(neu,m)}
  else{let c=neu;if(m)c='(async function(){'+c.replace(/^\s*(import|export)\b.*$/mg,'')+'\n})';new vm.Script(c);r[k]=true}
 }catch(e){r[k]=false}}
process.stdout.write(JSON.stringify(r));
"""


class Checker:

    def __init__(self):
        self.node = shutil.which("node")
        self.cache: dict[str, bool] = {}

    def check_many(self, items):
        todo = {k: v for k, v in items.items() if k not in self.cache}
        if todo and self.node:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "in.json"
                p.write_text(json.dumps(todo), "utf-8")
                js = Path(d) / "v.js"
                js.write_text(VERIFY_JS, "utf-8")
                out = subprocess.run([self.node, str(js), str(p)], capture_output=True, text=True, timeout=1800)
                res = json.loads(out.stdout or "{}")
                for k in todo:
                    self.cache[k] = bool(res.get(k, False))
        for k in todo:
            self.cache.setdefault(k, False)
        return self.cache


def _key(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "surrogatepass")).hexdigest()


def process_html(text: str, noindex: bool, js_ok) -> str:
    def blk(m):
        open_tag, tag, body, close = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if tag == "style":
            return open_tag + strip_css(body) + close
        t = TYPE_ATTR.search(open_tag)
        typ = (t.group(1).lower() if t else "text/javascript")
        if "json" in typ or "template" in typ or "html" in typ or "src=" in open_tag.lower() and not body.strip():
            return m.group(0)
        new = strip_js(body)
        return open_tag + (new if js_ok(body, new, typ == "module") else body) + close

    parts = []
    pos = 0
    for m in BLOCK.finditer(text):
        parts.append(("html", text[pos:m.start()]))
        parts.append(("blk", m))
        pos = m.end()
    parts.append(("html", text[pos:]))
    out = []
    for kind, v in parts:
        if kind == "html":
            out.append(HTML_COMMENT.sub(lambda mm: mm.group(0) if KEEP_HTML_COMMENT.match(mm.group(0)) else "", v))
        else:
            out.append(blk(v))
    res = "".join(out)
    if noindex and NOINDEX not in res:
        res = HEAD.sub(lambda mm: mm.group(0) + NOINDEX, res, count=1)
    return res


def main(argv: list[str]) -> int:
    noindex = "--noindex" in argv
    max_mb = None
    if "--max-mb" in argv:
        max_mb = float(argv[argv.index("--max-mb") + 1])
    targets = [a for a in argv if not a.startswith("--") and not re.fullmatch(r"[\d.]+", a)]
    files: list[Path] = []
    for t in targets:
        p = Path(t)
        files += [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()]
    chk = Checker()
    pending: dict[str, tuple[str, str, bool]] = {}

    def collect(orig, new, module):
        if orig != new:
            pending[_key(orig + "\0" + new)] = (orig, new, module)
        return True

    for f in files:
        if f.suffix == ".html":
            process_html(f.read_text("utf-8", "surrogatepass"), noindex, collect)
        elif f.suffix in (".js", ".mjs") and not f.name.endswith(".min.js"):
            src = f.read_text("utf-8", "surrogatepass")
            collect(src, strip_js(src), f.suffix == ".mjs")
    ok = chk.check_many(pending)

    def decide(orig, new, module):
        return orig == new or ok.get(_key(orig + "\0" + new), False)

    total = 0
    bad = 0
    for f in files:
        if f.suffix == ".html":
            src = f.read_text("utf-8", "surrogatepass")
            new = process_html(src, noindex, decide)
            if new != src:
                f.write_text(new, "utf-8", "surrogatepass")
        elif f.suffix in (".js", ".mjs") and not f.name.endswith(".min.js"):
            src = f.read_text("utf-8", "surrogatepass")
            new = strip_js(src)
            if new != src:
                if decide(src, new, False):
                    f.write_text(new, "utf-8", "surrogatepass")
                else:
                    bad += 1
        elif f.suffix == ".css":
            src = f.read_text("utf-8", "surrogatepass")
            new = strip_css(src)
            if new != src:
                f.write_text(new, "utf-8", "surrogatepass")
        total += f.stat().st_size
    kept = sum(1 for v in ok.values() if not v)
    print(f"stripped {len(files)} files, {total / 1048576:.0f} MB, kept-original JS blocks {kept + bad}")
    if max_mb is not None:
        mb = total / 1048576
        if mb > max_mb:
            print(f"::error::site is {mb:.0f} MB (> {max_mb:.0f} MB); not publishing")
            return 1
        if mb > max_mb * 0.9:
            print(f"::warning::site is {mb:.0f} MB, close to the {max_mb:.0f} MB limit")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
