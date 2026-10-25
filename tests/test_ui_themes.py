"""Appearance preferences are local, durable and independent of task state."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


HTML_PATH = Path(__file__).resolve().parents[1] / "src/rsci/ui/static/index.html"


def test_theme_preferences_and_first_paint():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js needed for frontend behavior tests")
    program = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.match(/<script id="theme-init">([\s\S]*?)<\/script>/)[1];
assert.ok(html.indexOf('id="theme-init"') < html.indexOf('<style>'));
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
function boot(initial, dark = true, blocked = false, legacy = false, noMedia = false) {
  const stored = new Map(initial ? [['rsci.ui.theme', initial]] : []);
  const root = {dataset:{},style:{}};
  const controls = {themeselect:{value:''},themestatus:{textContent:''}};
  const events = {}, media = {matches:dark};
  if (legacy) media.addListener = fn => events.media = fn;
  else media.addEventListener = (_, fn) => events.media = fn;
  let ready = false;
  const ctx = {
    document:{documentElement:root, getElementById:id=>ready ? controls[id] : null,
      addEventListener:(name,fn)=>events[name]=fn},
    localStorage:{getItem:key=>{if(blocked) throw Error('denied'); return stored.get(key);},
      setItem:(key,value)=>{if(blocked) throw Error('quota'); stored.set(key,value);}},
    window:{addEventListener:(name,fn)=>events[name]=fn}
  };
  if (!noMedia) ctx.window.matchMedia = () => media;
  vm.createContext(ctx); vm.runInContext(source, ctx);
  // The root already has its final theme before the controls even exist.
  assert.ok(root.dataset.theme);
  ready = true; events.DOMContentLoaded();
  return {stored,root,controls,events,media,theme:ctx.window.RsciTheme};
}
const fresh = boot();
assert.equal(fresh.root.dataset.theme,'dark');
assert.equal(fresh.controls.themeselect.value,'dark');
assert.equal(fresh.stored.size,0); // merely opening a tab must not rewrite preferences
for (const name of ['dark','light','sand','forest']) {
  fresh.theme.setPreference(name);
  assert.equal(fresh.root.dataset.theme,name);
  assert.equal(fresh.stored.get('rsci.ui.theme'),name);
  assert.equal(fresh.controls.themeselect.value,name);
  const reload = boot(fresh.stored.get('rsci.ui.theme'));
  assert.equal(reload.root.dataset.theme,name);
  assert.equal(reload.root.style.colorScheme,['light','sand'].includes(name)?'light':'dark');
}
for (const legacy of [true,false]) {
  const follow = boot('system',false,false,legacy);
  assert.equal(follow.root.dataset.theme,'light');
  assert.equal(follow.controls.themeselect.value,'system');
  follow.media.matches=true; follow.events.media();
  assert.equal(follow.root.dataset.theme,'dark');
  assert.equal(follow.stored.get('rsci.ui.theme'),'system');
  follow.theme.setPreference('sand'); follow.media.matches=false; follow.events.media();
  assert.equal(follow.root.dataset.theme,'sand'); // system changes respect explicit choice
  follow.events.storage({key:'rsci.chat.draft.A',newValue:'ignored'});
  assert.equal(follow.root.dataset.theme,'sand');
  follow.events.storage({key:'rsci.ui.theme',newValue:'forest'});
  assert.equal(follow.root.dataset.theme,'forest');
  assert.equal(follow.controls.themeselect.value,'forest');
  follow.events.storage({key:'rsci.ui.theme',newValue:null});
  assert.equal(follow.root.dataset.theme,'dark');
  follow.theme.setPreference('light'); follow.events.storage({key:null,newValue:null});
  assert.equal(follow.root.dataset.theme,'dark');
}
assert.equal(boot('<invalid>').root.dataset.theme,'dark');
fresh.theme.setPreference('__proto__'); assert.equal(fresh.root.dataset.theme,'dark');
assert.equal(boot('system',true,false,false,true).root.dataset.theme,'dark');
const denied = boot('light',true,true);
denied.theme.setPreference('forest');
assert.equal(denied.root.dataset.theme,'forest');
assert.ok(denied.controls.themestatus.textContent.includes('无法保存'));
assert.equal(denied.stored.get('rsci.ui.theme'),'light'); // no writes succeeded
console.log('PASS: first paint, persistence, system changes, multi-tab and unavailable storage');
"""
    result = subprocess.run([node, "-e", program, str(HTML_PATH)], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr


def _luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                for v in channels]
    return sum(v * weight for v, weight in zip(channels, (.2126, .7152, .0722)))


@pytest.mark.parametrize("theme", ["dark", "light", "sand", "forest"])
def test_theme_readability(theme):
    html = HTML_PATH.read_text(encoding="utf-8")
    base = re.search(r":root\{([^}]+)\}", html).group(1)
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", base))
    if theme != "dark":
        override = re.search(r':root\[data-theme="' + theme + r'"\]\{([^}]+)\}', html).group(1)
        tokens.update(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})", override))
    pairs = [(fg, bg, 4.5) for fg in ("--fg", "--fg2", "--acc", "--err", "--ok", "--warn")
             for bg in ("--bg", "--bg2", "--bg3")]
    pairs += [(fg, bg, 4.5) for fg, bg in [
        ("--action-fg", "--action-bg"), ("--action-fg", "--danger-bg"),
        ("--ok", "--ok-bg"), ("--err", "--err-bg"),
        ("--warn", "--warn-bg"), ("--acc", "--info-bg"),
        ("--avatar-fg", "--avatar-bg"), ("--fg", "--user-bg"),
        ("--code", "--bg3"), ("--fg2", "--confirm-bg"),
        ("--brand-ink", "--brand-start"), ("--brand-ink", "--brand-end"),
    ]]
    pairs += [("--control-line", bg, 3) for bg in ("--bg", "--bg2", "--bg3")]
    for fg, bg, minimum in pairs:
        first, second = sorted((_luminance(tokens[fg]), _luminance(tokens[bg])))
        contrast = (second + .05) / (first + .05)
        assert contrast >= minimum, f"{theme} {fg}/{bg}: {contrast:.2f} < {minimum}"


def test_theme_selector_is_labelled_and_palette_covers_special_surfaces():
    html = HTML_PATH.read_text(encoding="utf-8")
    assert '<label for="themeselect">外观主题</label>' in html
    assert 'aria-describedby="themestatus"' in html
    for value in ("dark", "light", "sand", "forest", "system"):
        assert f'<option value="{value}">' in html
    for token in ("--confirm-bg", "--user-bg", "--avatar-bg", "--ok-bg", "--err-bg",
                  "--warn-bg", "--info-bg", "--code", "--memory", "--overlay"):
        assert f"var({token})" in html
    assert "仅改变外观" in html
    # The stop button has higher specificity than the shared danger rule.
    assert "#sendbtn.danger{background:var(--danger-bg);" in html
