#!/usr/bin/env python3
"""
Build a self-contained HTML demo of the Weekend Dispatch as it arrives.

A mock inbox holds the dispatch email. Opening it shows the real envelope
email; its button opens the dispatch in a browser frame, and every link
between the pages (the section bar, previous/next, the Fort Greene Community
News page, the subway changes, the long list) works. Article links open the real sources in a new
tab. A Phone / Desktop switch shows both layouts.

It is built from a run of the dispatch, so what it shows is that run's pages:

    python weekend_dispatch.py --preview --site-dir site_preview
    python build_demo.py --site-dir site_preview --out inbox-demo.html

Run it from the repository root (the envelope photo lives in assets/). The demo
is for looking at only; it sends nothing and writes nothing but the one file.
"""

import argparse
import base64
import datetime
import json
import pathlib
import re

import weekend_dispatch as wd


def next_saturday(today=None):
    today = today or datetime.date.today()
    return today + datetime.timedelta(days=(5 - today.weekday()) % 7)


def build(site_dir, out, saturday, notice):
    site = pathlib.Path(site_dir)
    pages = {p.name: p.read_text(encoding="utf-8") for p in sorted(site.glob("*.html"))}
    if wd.dp.HOME_FILE not in pages:
        raise SystemExit(f"No {wd.dp.HOME_FILE} in {site}: run the dispatch with --preview first.")
    sunday = saturday + datetime.timedelta(days=1)

    # The email, exactly as the dispatch builds it; the photo is inlined so the
    # demo is one file, and the button points at the front page.
    parts = wd.build_envelope_parts(saturday, sunday, wd.dp.HOME_FILE, inline=True)   # the animated GIF the email carries
    envelope = parts["html"]
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", wd.ENVELOPE_BLURB))
    snippet = snippet.replace("&mdash;", "—").replace("&amp;", "&").replace("&rsquo;", "’")

    def subject(d):
        return f"Weekend Dispatch · {d.strftime('%B %d')}"

    titles = {"weekend.html": "The dispatch (main page)", wd.dp.NEWS_FILE: "News & Culture",
              "events.html": "The Long List", wd.mw.SUBWAY_FILE: "Subway changes",
              wd.dp.CHCO_FILE: "Fort Greene Community News"}
    meta = {
        "sender": "Sample Dispatch Desk",
        "subject": subject(saturday),
        "when": saturday.strftime("%a, %b %-d, 1:15 AM"),
        "short_when": "1:15 AM",
        "snippet": snippet,
        "older": [{"subject": subject(saturday - datetime.timedelta(days=7 * k)),
                   "when": (saturday - datetime.timedelta(days=7 * k)).strftime("%b %-d")} for k in (1, 2, 3)],
        "titles": titles,
        "order": [f for f in titles if f in pages],
        "notice": notice,
        "built": datetime.date.today().strftime("%B %-d, %Y"),
    }

    def js(obj):
        # safe inside a <script> element
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")

    html = TEMPLATE.replace("__PAGES__", js(pages)).replace("__ENVELOPE__", js(envelope)).replace("__META__", js(meta))
    pathlib.Path(out).write_text(html, encoding="utf-8")
    print(f"Wrote {out}: {len(html) / 1024:.0f} KB, {len(pages)} pages")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Weekend Dispatch &middot; inbox demo</title>
<style>
  :root { --bg:#eef1f6; --panel:#fff; --line:#e3e6ec; --ink:#1f2328; --muted:#5f6b7a; --accent:#9B3A1A; --sel:#d8e4fb; --unread:#fff; --read:#f4f6fa; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--ink); background: #cfd5df;
         display: flex; flex-direction: column; align-items: center; }
  button { font: inherit; color: inherit; cursor: pointer; }
  .notice { width: 100%; background: #1A1208; color: #F5EDD8; padding: 8px 16px; font-size: 12.5px; display: flex; gap: 12px; align-items: center; justify-content: center; text-align: center; }
  .notice button { background: transparent; border: 1px solid #8A6A3A; color: #D4B98A; border-radius: 4px; padding: 2px 8px; font-size: 12px; }
  .toolbar { display: flex; gap: 8px; align-items: center; padding: 12px 16px; }
  .seg { display: inline-flex; border: 1px solid #9aa4b4; border-radius: 8px; overflow: hidden; background: #fff; }
  .seg button { border: 0; background: transparent; padding: 7px 14px; }
  .seg button[aria-pressed="true"] { background: #1f2328; color: #fff; }
  .link { background: transparent; border: 0; color: #1f2328; text-decoration: underline; padding: 7px 8px; }
  .stage { flex: 1; width: 100%; display: flex; justify-content: center; align-items: flex-start; padding: 0 16px 20px; min-height: 0; }
  .device { position: relative; width: min(100%, 1180px); height: calc(100vh - 118px); min-height: 560px; background: var(--bg); border-radius: 14px; overflow: hidden;
            box-shadow: 0 18px 50px rgba(20,30,50,.35); display: flex; flex-direction: column; }
  body.phone .device { width: 390px; height: min(820px, calc(100vh - 118px)); border-radius: 40px; border: 10px solid #101216; box-shadow: 0 18px 50px rgba(0,0,0,.45); }
  .statusbar { display: none; height: 26px; padding: 6px 22px 0; font-size: 12px; font-weight: 600; justify-content: space-between; background: var(--bg); }
  body.phone .statusbar { display: flex; }

  /* mail app */
  .app { flex: 1; display: flex; min-height: 0; }
  .side { width: 220px; padding: 12px 8px; flex: none; }
  body.phone .side { display: none; }
  .compose { background: #c2e7ff; border: 0; border-radius: 14px; padding: 14px 22px; font-weight: 600; margin: 0 0 14px 6px; opacity: .8; }
  .folder { display: flex; justify-content: space-between; padding: 7px 14px; border-radius: 0 16px 16px 0; color: #202124; }
  .folder.active { background: var(--sel); font-weight: 700; }
  .main { flex: 1; min-width: 0; margin: 0 12px 12px 0; background: var(--panel); border-radius: 16px; overflow: hidden; display: flex; flex-direction: column; }
  body.phone .main { margin: 0; border-radius: 0; }
  .searchbar { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; gap: 10px; align-items: center; }
  .searchbar input { flex: 1; border: 0; background: #eaf1fb; border-radius: 22px; padding: 10px 16px; font: inherit; outline: none; }
  .searchbar input:focus { box-shadow: 0 0 0 2px #7aa7f2; }
  .list { overflow: auto; flex: 1; }
  .row { display: grid; grid-template-columns: 26px 170px 1fr auto; gap: 10px; align-items: center; padding: 9px 16px; border-bottom: 1px solid var(--line); background: var(--read); width: 100%; text-align: left; border-left: 0; border-right: 0; border-top: 0; }
  .row.unread { background: var(--unread); font-weight: 700; }
  .row:hover { box-shadow: inset 1px 0 #7aa7f2, 0 1px 3px rgba(0,0,0,.18); }
  .row .star { font-size: 17px; color: #aab; line-height: 1; background: none; border: 0; padding: 0; }
  .row .star.on { color: #e8a800; }
  .row .snip { color: var(--muted); font-weight: 400; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .subj { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .row .when { font-size: 12px; color: var(--muted); white-space: nowrap; }
  .row.unread .when { color: var(--ink); }
  .row.disabled { cursor: default; }
  body.phone .row { grid-template-columns: 1fr auto; row-gap: 2px; }
  body.phone .row .star { display: none; }
  body.phone .row .from { grid-column: 1; }
  body.phone .row .subj { grid-column: 1 / 3; }
  body.phone .row .snip { grid-column: 1 / 3; }
  .empty { padding: 40px; text-align: center; color: var(--muted); }
  .reader { display: none; flex: 1; min-height: 0; flex-direction: column; }
  .reader.on { display: flex; }
  .rbar { padding: 8px 12px; border-bottom: 1px solid var(--line); display: flex; align-items: center; gap: 10px; }
  .rbar button { background: none; border: 0; padding: 6px 10px; border-radius: 8px; font-size: 18px; }
  .rbar button:hover { background: #eef1f6; }
  .rscroll { overflow: auto; flex: 1; }
  .rhead { padding: 16px 22px 8px; }
  .rhead h1 { font-size: 21px; font-weight: 500; margin: 0 0 10px; }
  .from-row { display: flex; align-items: center; gap: 12px; }
  .avatar { width: 38px; height: 38px; border-radius: 50%; background: #1A1208; color: #D4B98A; display: grid; place-items: center; font-weight: 700; flex: none; }
  .from-row .who { font-weight: 600; }
  .from-row .to { color: var(--muted); font-size: 12.5px; }
  .from-row .when { margin-left: auto; color: var(--muted); font-size: 12.5px; }
  .hint { margin: 6px 22px 0; padding: 8px 12px; background: #fff7df; border: 1px solid #f0dca0; border-radius: 8px; color: #6b5300; font-size: 12.5px; }
  #email { display: block; width: 100%; border: 0; min-height: 520px; }

  /* the newspaper opens in a browser frame */
  .browser { position: absolute; inset: 0; background: #fff; display: none; flex-direction: column; z-index: 5; }
  .browser.on { display: flex; }
  .bbar { display: flex; align-items: center; gap: 6px; padding: 8px; background: #e9ecf1; border-bottom: 1px solid #cfd5df; }
  .bbar button { border: 0; background: transparent; border-radius: 8px; padding: 6px 9px; font-size: 16px; line-height: 1; }
  .bbar button:hover:not(:disabled) { background: #d6dbe4; }
  .bbar button:disabled { opacity: .35; cursor: default; }
  .addr { flex: 1; min-width: 0; background: #fff; border-radius: 16px; padding: 6px 14px; font-size: 12.5px; color: #3c4655; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .addr b { color: #1f2328; font-weight: 600; }
  .menu { position: absolute; right: 8px; top: 46px; background: #fff; border: 1px solid #cfd5df; border-radius: 10px; box-shadow: 0 10px 28px rgba(0,0,0,.25); padding: 6px; display: none; z-index: 8; min-width: 250px; max-height: 70%; overflow: auto; }
  .menu.on { display: block; }
  .menu button { display: block; width: 100%; text-align: left; border: 0; background: none; padding: 7px 10px; border-radius: 6px; }
  .menu button:hover { background: #eef1f6; }
  .menu button[aria-current="true"] { font-weight: 700; background: #f3e6df; }
  .menu hr { border: 0; border-top: 1px solid var(--line); margin: 5px 0; }
  .menu small { display: block; padding: 6px 10px 3px; color: var(--muted); }
  #page { flex: 1; width: 100%; border: 0; background: #EDE0C0; }
  .toast { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); background: #1f2328; color: #fff; padding: 9px 16px; border-radius: 18px; font-size: 12.5px; opacity: 0; pointer-events: none; transition: opacity .2s; z-index: 9; max-width: 90%; text-align: center; }
  .toast.on { opacity: .95; }
  @media (prefers-reduced-motion: reduce) { .toast { transition: none; } }
</style>
</head>
<body>
<div class="notice" id="notice"><span id="noticeText"></span><button id="noticeClose" type="button">Dismiss</button></div>
<div class="toolbar">
  <div class="seg" role="group" aria-label="Device">
    <button type="button" id="btnDesktop" aria-pressed="true">Desktop</button>
    <button type="button" id="btnPhone" aria-pressed="false">Phone</button>
  </div>
  <button type="button" class="link" id="btnReset">Reset demo</button>
</div>
<div class="stage">
  <div class="device" id="device">
    <div class="statusbar"><span>9:41</span><span>&#9679;&#9679;&#9679;</span></div>
    <div class="app">
      <aside class="side">
        <button class="compose" type="button" disabled>Compose</button>
        <div class="folder active"><span>Inbox</span><span id="unreadCount">1</span></div>
        <div class="folder"><span>Starred</span></div>
        <div class="folder"><span>Sent</span></div>
        <div class="folder"><span>Drafts</span></div>
      </aside>
      <section class="main">
        <div id="listView" style="display:flex;flex-direction:column;flex:1;min-height:0">
          <div class="searchbar"><input id="search" type="search" placeholder="Search mail" aria-label="Search mail"></div>
          <div class="list" id="list"></div>
        </div>
        <div class="reader" id="reader">
          <div class="rbar"><button type="button" id="btnBack" aria-label="Back to inbox">&larr;</button></div>
          <div class="rscroll">
            <div class="rhead">
              <h1 id="rSubject"></h1>
              <div class="from-row"><div class="avatar">T</div>
                <div><div class="who" id="rWho"></div><div class="to">to me</div></div>
                <div class="when" id="rWhen"></div></div>
            </div>
            <div class="hint">Try it: click <b>Open Full Newsletter</b> in the email. Everything inside works, and article links open the real source in a new tab.</div>
            <iframe id="email" title="The dispatch email"></iframe>
          </div>
        </div>
      </section>
    </div>
    <div class="browser" id="browser">
      <div class="bbar">
        <button type="button" id="bClose" aria-label="Close and return to the email">&#10005;</button>
        <button type="button" id="bBack" aria-label="Back" disabled>&larr;</button>
        <button type="button" id="bFwd" aria-label="Forward" disabled>&rarr;</button>
        <button type="button" id="bHome" aria-label="Front page">&#8962;</button>
        <div class="addr" id="addr"></div>
        <button type="button" id="bMenu" aria-label="All pages" aria-expanded="false">&#9776;</button>
      </div>
      <div class="menu" id="menu"></div>
      <iframe id="page" title="The dispatch"></iframe>
    </div>
    <div class="toast" id="toast" role="status"></div>
  </div>
</div>

<script>
const PAGES = __PAGES__;
const ENVELOPE = __ENVELOPE__;
const META = __META__;
const $ = id => document.getElementById(id);
const state = { opened: false, starred: false, hist: [], i: -1 };

// ---- chrome
$('noticeText').textContent = META.notice;
$('noticeClose').onclick = () => $('notice').style.display = 'none';
function setDevice(phone) {
  document.body.classList.toggle('phone', phone);
  $('btnPhone').setAttribute('aria-pressed', phone); $('btnDesktop').setAttribute('aria-pressed', !phone);
  setTimeout(fitEmail, 50);
}
$('btnDesktop').onclick = () => setDevice(false);
$('btnPhone').onclick = () => setDevice(true);
let toastTimer;
function toast(msg) { const t = $('toast'); t.textContent = msg; t.classList.add('on'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 2400); }

// ---- inbox
function rowHtml(r, idx) {
  return `<button class="row ${r.unread ? 'unread' : ''} ${r.live ? '' : 'disabled'}" data-i="${idx}" type="button">
    <span class="star ${r.starred ? 'on' : ''}" role="button" aria-label="Star" data-star="${idx}">${r.starred ? '★' : '☆'}</span>
    <span class="from">${r.from}</span><span class="subj"><span>${r.subject}</span> <span class="snip">&mdash; ${r.snippet}</span></span>
    <span class="when">${r.when}</span></button>`;
}
let rows = [];
function resetRows() {
  rows = [{ from: META.sender, subject: META.subject, snippet: META.snippet, when: META.short_when, unread: true, live: true, starred: false }]
    .concat(META.older.map(o => ({ from: META.sender, subject: o.subject, snippet: 'Your weekend dispatch has arrived', when: o.when, unread: false, live: false })));
}
function renderList() {
  const q = $('search').value.trim().toLowerCase();
  const shown = rows.map((r, i) => [r, i]).filter(([r]) => !q || (r.from + r.subject + r.snippet).toLowerCase().includes(q));
  $('list').innerHTML = shown.length ? shown.map(([r, i]) => rowHtml(r, i)).join('') : '<div class="empty">No messages match your search.</div>';
  $('unreadCount').textContent = rows.filter(r => r.unread).length || '';
  document.title = (rows[0].unread ? '(1) ' : '') + 'Weekend Dispatch · inbox demo';
}
$('search').oninput = renderList;
$('list').onclick = e => {
  const star = e.target.closest('[data-star]');
  if (star) { e.stopPropagation(); const r = rows[+star.dataset.star]; r.starred = !r.starred; renderList(); return; }
  const row = e.target.closest('.row'); if (!row) return;
  const r = rows[+row.dataset.i];
  if (!r.live) { toast('Demo: only the newest issue is wired up.'); return; }
  openMessage();
};

// ---- the message
function fitEmail() {
  const f = $('email');
  try { const d = f.contentDocument; if (d && d.documentElement) f.style.height = (d.documentElement.scrollHeight + 4) + 'px'; } catch (e) {}
}
function openMessage() {
  rows[0].unread = false; renderList();
  $('rSubject').textContent = META.subject; $('rWho').textContent = META.sender; $('rWhen').textContent = META.when;
  $('listView').style.display = 'none'; $('reader').classList.add('on');
  if (!$('email').srcdoc) $('email').srcdoc = ENVELOPE;
  state.opened = true; $('reader').querySelector('.rscroll').scrollTop = 0;
}
function closeMessage() { $('reader').classList.remove('on'); $('listView').style.display = 'flex'; state.opened = false; }
$('btnBack').onclick = closeMessage;
$('email').addEventListener('load', () => {
  fitEmail(); setTimeout(fitEmail, 400);
  const d = $('email').contentDocument;
  d.addEventListener('keydown', onKey);
  d.addEventListener('click', e => { const a = e.target.closest('a'); if (!a) return; e.preventDefault(); openBrowser('weekend.html'); });
});

// ---- the newspaper
function show(file) {
  $('page').srcdoc = PAGES[file];
  $('addr').innerHTML = 'weekend-dispatch / <b>' + file + '</b>';
  document.querySelectorAll('#menu button[data-f]').forEach(b => b.setAttribute('aria-current', b.dataset.f === file));
  $('bBack').disabled = state.i <= 0; $('bFwd').disabled = state.i >= state.hist.length - 1;
}
function go(file) {
  if (!PAGES[file]) return;
  state.hist = state.hist.slice(0, state.i + 1); state.hist.push(file); state.i = state.hist.length - 1; show(file);
}
function openBrowser(file) { $('browser').classList.add('on'); state.hist = []; state.i = -1; go(file); }
function closeBrowser() { $('browser').classList.remove('on'); $('menu').classList.remove('on'); }
$('bClose').onclick = closeBrowser;
$('bBack').onclick = () => { if (state.i > 0) { state.i--; show(state.hist[state.i]); } };
$('bFwd').onclick = () => { if (state.i < state.hist.length - 1) { state.i++; show(state.hist[state.i]); } };
$('bHome').onclick = () => go('weekend.html');
$('bMenu').onclick = () => { const on = $('menu').classList.toggle('on'); $('bMenu').setAttribute('aria-expanded', on); };
$('menu').innerHTML = '<small>Every page in this edition</small>' + META.order.map(f =>
  `<button type="button" data-f="${f}">${META.titles[f]}</button>`).join('');
$('menu').onclick = e => { const b = e.target.closest('button[data-f]'); if (!b) return; $('menu').classList.remove('on'); go(b.dataset.f); };
$('page').addEventListener('load', () => {
  $('menu').classList.remove('on');
  const d = $('page').contentDocument; if (!d) return;
  d.addEventListener('keydown', onKey);
  d.addEventListener('click', e => {
    const a = e.target.closest('a'); if (!a) return;
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#')) return;
    const base = href.split('#')[0].split('?')[0];
    if (PAGES[base]) { e.preventDefault(); go(base); }
    else if (/^https?:/.test(href)) {
      e.preventDefault(); let host = href; try { host = new URL(href).hostname; } catch (x) {}
      toast('Opens ' + host + ' in a new tab'); window.open(href, '_blank', 'noopener');
    }
  });
});

// ---- keyboard and reset
function onKey(e) {
  if (e.key !== 'Escape') return;
  if ($('menu').classList.contains('on')) $('menu').classList.remove('on');
  else if ($('browser').classList.contains('on')) closeBrowser();
  else if (state.opened) closeMessage();
}
document.addEventListener('keydown', onKey);
$('btnReset').onclick = () => { closeBrowser(); closeMessage(); resetRows(); $('search').value = ''; renderList(); toast('Demo reset'); };
resetRows(); renderList(); setDevice(false);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the inbox demo from a dispatch preview run.")
    ap.add_argument("--site-dir", default="site_preview", help="Folder of pages written by --preview.")
    ap.add_argument("--out", default="inbox-demo.html", help="The single HTML file to write.")
    ap.add_argument("--saturday", help="Edition date, YYYY-MM-DD (default: the coming Saturday).")
    ap.add_argument("--notice", default=(
        "Demo built from live data. The weather, Greenmarket and Brooklyn-picks paragraphs are sample text written "
        "for this preview; the real dispatch has Claude write them each Saturday."),
        help="The banner shown above the demo.")
    a = ap.parse_args()
    sat = datetime.date.fromisoformat(a.saturday) if a.saturday else next_saturday()
    build(a.site_dir, a.out, sat, a.notice)
