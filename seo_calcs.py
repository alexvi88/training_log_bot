"""Калькуляторы третьей части сайта (каркас — seo_pages._render_tool):
тоннаж тренировки, таймер отдыха, «когда я пожму 100?» и «насколько ты
сильный» на фоне атлетов с помоста. Каждый — (форма, результат, скрипт), как
калькуляторы первой части; математика — в seo_tools (и её JS-двойник в
CALC_JS), тексты — `seo.tool.<имя>.*` в каталоге.

- Тоннаж: сумма вес × повторы × подходы и сравнение «это как N слонов» —
  тем же списком предметов и теми же строками каталога, что бот пишет под
  итогом тренировки (`formatting.format_tonnage_equivalent`, seed 0 — самый
  лёгкий подходящий предмет). Формы слов для чисел браузер берёт из
  Intl.PluralRules; сами слова — из каталога, по одному на категорию.
- Таймер отдыха: обратный отсчёт по часам (`Date.now`), а не по тикам —
  свёрнутая вкладка не отстаёт; в конце звук (WebAudio), вибрация, где
  браузер её даёт, и по желанию — экран не гаснет (Wake Lock).
- Цель: прогноз `seo_tools.goal_curve` двумя концами (быстрый и медленный
  темп) — диапазон дат, а не день; карточка для сторис — `/og/card/…`.
- Перцентиль: таблица `seo_data/strength_percentiles.json` (OpenPowerlifting,
  классика, USAPL) — сравнение с теми, кто выступает, а не со всем залом.

Карточки `/og/card/<язык>/goal.png` и `/pct.png` рисует тот же
seo_static.render_og, что и превью страниц; параметры строго проверяются
(числа в границах), текст — из каталога, кэш — тот же og_png.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

import config
import formatting
import i18n
import seo_pages as sp
import seo_static
import seo_tools

# ---------- тоннаж ----------

_PLURAL_SAMPLES = {"ru": {"one": 1, "few": 2, "many": 5, "other": 1.5}, "en": {"one": 1, "other": 2}}


def _tonnage_objects(lang: str) -> list[dict]:
    """Предметы бота с формами слова по категориям множественного числа."""
    out = []
    for emoji, kg, obj_id in formatting._TONNAGE_OBJECTS:
        forms = {cat: i18n.t_in(lang, f"tonnage.object.{obj_id}", n=n) for cat, n in _PLURAL_SAMPLES[lang].items()}
        out.append({"e": emoji, "kg": kg, "f": forms})
    return out


def tool_tonnage(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    row = (
        "<div class=set-row>"
        + sp._num_input("w1", t("seo.tool.1rm.weight"), "80", "0.25")
        + sp._num_input("r1", t("seo.tool.1rm.reps"), "8", "1", "numeric")
        + sp._num_input("s1", t("seo.tool.tonnage.sets"), "3", "1", "numeric")
        + "</div>"
    )
    form = (
        "<form class=calc onsubmit='return false'>"
        f"<div class=lbl>{sp._e(t('seo.tool.tonnage.rows'))}</div><div id=rows>{row}</div>"
        f'<div class=btn-row><button class="btn small" type=button id=add>{sp._e(t("seo.tool.tonnage.add"))}</button>'
        f'<button class="btn small" type=button id=del>{sp._e(t("seo.tool.tonnage.remove"))}</button></div>'
        + sp._unit_seg(lang) + "</form>"
    )
    result = (
        f'<div class=result aria-live=polite><div class=k>{sp._e(t("seo.tool.tonnage.result"))}</div>'
        f'<div class=big id=tot>—</div><div class=note id=eq></div>'
        f'<div class=pair><div><div class=k>{sp._e(t("seo.tool.tonnage.total_sets"))}</div><div class=big id=ns>—</div></div>'
        f'<div><div class=k>{sp._e(t("seo.tool.tonnage.total_reps"))}</div><div class=big id=nr>—</div></div>'
        f'<div><div class=k>{sp._e(t("seo.tool.tonnage.avg"))}</div><div class=big id=avg>—</div></div></div>'
        + ("" if embed else sp._log_cta(lang, "tonnage")) + "</div>"
    )
    script = (
        f"var OBJ={json.dumps(_tonnage_objects(lang), ensure_ascii=False)},PR=new Intl.PluralRules({json.dumps(lang)}),"
        f"EQ={sp._js_str(i18n.t_in(lang, 'tonnage.equivalent', count='{count}', noun='{noun}', emoji='{emoji}'))},"
        f"TON={sp._js_str(t('seo.tool.unit_t'))},LBK=C.lbPerKg;"
        "function equiv(total,u){var kg=u==='lb'?total/LBK:total;if(kg<150)return '';var c=[];"
        "OBJ.forEach(function(o){var n=Math.round(kg/o.kg);if(n>=2&&n<=40)c.push([o,n]);});"
        "if(!c.length){var f=OBJ.filter(function(o){return o.kg<=kg;});if(!f.length)return '';var o=f[f.length-1];c=[[o,Math.max(1,Math.round(kg/o.kg))]];}"
        "var o=c[0][0],n=c[0][1],cat=PR.select(n),noun=o.f[cat]||o.f.other;"
        "return EQ.replace('{count}',n).replace('{noun}',noun).replace('{emoji}','').trim();}"
        "function tonText(total,u){if(u==='kg'&&total>=1000)return fmt(total/1000)+'\u00a0<span class=u>'+TON+'</span>';return fu(total,u);}"
        "var n=1;function rowHtml(i){return document.querySelector('#rows .set-row').outerHTML.replace(/(w|r|s)1/g,'$1'+i);}"
        "$('add').addEventListener('click',function(){if(n>=30)return;n++;var d=document.createElement('div');d.innerHTML=rowHtml(n);"
        "var el=d.firstChild;el.querySelectorAll('input').forEach(function(x){x.addEventListener('input',run);});$('rows').appendChild(el);run();});"
        "$('del').addEventListener('click',function(){if(n<=1)return;$('rows').lastChild.remove();n--;run();});"
        "function run(){var u=val('unit'),tot=0,sets=0,reps=0;for(var i=1;i<=n;i++){var w=parseFloat($('w'+i).value)||0,"
        "r=parseInt($('r'+i).value,10)||0,s=parseInt($('s'+i).value,10)||0;if(r>0&&s>0){tot+=w*r*s;sets+=s;reps+=r*s;}}"
        "if(!(tot>0)){$('tot').textContent=$('ns').textContent=$('nr').textContent=$('avg').textContent='—';$('eq').textContent='';done(0);return;}"
        "$('tot').innerHTML=tonText(tot,u);$('ns').textContent=sets;$('nr').textContent=reps;$('avg').innerHTML=fu(tot/reps,u);"
        "$('eq').textContent=equiv(tot,u);done(1);share('tonnage','v='+Math.round(tot)+'&u='+u);}"
        "bind(run);"
    )
    return form, result, script


# ---------- таймер отдыха ----------

REST_PRESETS = (60, 90, 120, 180)


def tool_rest_timer(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    presets = [(str(s), t("seo.tool.rest-timer.preset", s=s)) for s in REST_PRESETS]
    form = (
        "<form class=calc onsubmit='return false'>"
        + sp._seg("preset", t("seo.tool.rest-timer.presets"), presets, "90")
        + sp._num_input("custom", t("seo.tool.rest-timer.custom"), "90", "5", "numeric", "900")
        + "<div class=checks>"
        f"<label><input type=checkbox id=snd checked>{sp._e(t('seo.tool.rest-timer.sound'))}</label>"
        f"<label><input type=checkbox id=vib checked>{sp._e(t('seo.tool.rest-timer.vibrate'))}</label>"
        f"<label><input type=checkbox id=wake>{sp._e(t('seo.tool.rest-timer.wake'))}</label></div>"
        "</form>"
    )
    result = (
        f'<div class="result timer" aria-live=polite><div class=k>{sp._e(t("seo.tool.rest-timer.left"))}</div>'
        '<div class="big clock" id=clock role=timer>1:30</div>'
        '<div class=bar-track><div class=bar-fill id=fill></div></div>'
        f'<div class=btn-row><button class="btn primary" type=button id=go>{sp._e(t("seo.tool.rest-timer.start"))}</button>'
        f'<button class="btn" type=button id=plus>{sp._e(t("seo.tool.rest-timer.plus"))}</button>'
        f'<button class="btn" type=button id=reset>{sp._e(t("seo.tool.rest-timer.reset"))}</button></div>'
        '<div class=note id=note></div>'
        + ("" if embed else sp._log_cta(lang, "rest-timer", "seo.tool.rest-timer.log_cta")) + "</div>"
    )
    script = (
        f"var START={sp._js_str(t('seo.tool.rest-timer.start'))},PAUSE={sp._js_str(t('seo.tool.rest-timer.pause'))},"
        f"DONE={sp._js_str(t('seo.tool.rest-timer.done'))},SETS={sp._js_str(t('seo.tool.rest-timer.sets', n='{n}'))},"
        "TITLE=document.title;"
        "var total=90,left=90,end=0,tick=null,rounds=0,lock=null,ac=null;"
        "function mmss(s){s=Math.max(0,Math.ceil(s));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}"
        "function paint(){$('clock').textContent=mmss(left);$('fill').style.width=(total?Math.max(0,100*left/total):0)+'%';"
        "document.title=tick?mmss(left)+' · '+TITLE:TITLE;}"
        "function beep(){if(!$('snd').checked)return;try{ac=ac||new (window.AudioContext||window.webkitAudioContext)();"
        "[0,0.35,0.7].forEach(function(d){var o=ac.createOscillator(),g=ac.createGain();o.frequency.value=880;o.connect(g);g.connect(ac.destination);"
        "g.gain.setValueAtTime(0.25,ac.currentTime+d);g.gain.exponentialRampToValueAtTime(0.001,ac.currentTime+d+0.25);o.start(ac.currentTime+d);o.stop(ac.currentTime+d+0.26);});}catch(e){}}"
        "function buzz(){if($('vib').checked&&navigator.vibrate)navigator.vibrate([250,120,250,120,400]);}"
        "function wake(on){if(!('wakeLock' in navigator))return;if(on&&$('wake').checked&&!lock){navigator.wakeLock.request('screen').then(function(l){lock=l;}).catch(function(){});}"
        "if(!on&&lock){lock.release();lock=null;}}"
        "function stop(){clearInterval(tick);tick=null;$('go').textContent=START;wake(false);paint();}"
        "function step(){left=(end-Date.now())/1000;if(left<=0){left=0;stop();rounds++;beep();buzz();"
        "document.querySelector('.timer').classList.add('ring');$('note').textContent=DONE+' '+SETS.replace('{n}',rounds);return;}paint();}"
        "function startStop(){if(ac&&ac.state==='suspended')ac.resume();if(tick){left=(end-Date.now())/1000;stop();return;}"
        "if(left<=0)left=total;document.querySelector('.timer').classList.remove('ring');end=Date.now()+left*1000;"
        "tick=setInterval(step,200);$('go').textContent=PAUSE;wake(true);step();}"
        "function setTotal(s){if(!(s>0))return;total=s;left=s;if(tick){end=Date.now()+left*1000;}paint();}"
        "document.querySelectorAll('[name=preset]').forEach(function(el){el.addEventListener('change',function(){$('custom').value=this.value;setTotal(parseInt(this.value,10));});});"
        "$('custom').addEventListener('input',function(){var s=parseInt(this.value,10);if(s>0&&s<=900){document.querySelectorAll('[name=preset]').forEach(function(r){r.checked=r.value===String(s);});setTotal(s);}});"
        "$('go').addEventListener('click',startStop);"
        "$('plus').addEventListener('click',function(){left+=15;total=Math.max(total,left);if(tick)end+=15000;paint();});"
        "$('reset').addEventListener('click',function(){stop();left=total;document.querySelector('.timer').classList.remove('ring');$('note').textContent='';paint();});"
        "document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'&&tick){wake(false);wake(true);step();}});"
        "paint();done(1);"
    )
    return form, result, script


# ---------- когда я пожму 100? ----------

GOAL_LIFTS = ("bench", "squat", "deadlift", "other")
LEVELS = ("beginner", "intermediate", "advanced")


def tool_goal(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    lifts = [(k, t(f"seo.tool.goal.lift.{k}")) for k in GOAL_LIFTS]
    form = (
        "<form class=calc onsubmit='return false'>"
        + sp._seg("lift", t("seo.strength.tool.lift"), lifts, "bench")
        + sp._seg("mode", t("seo.tool.goal.mode"), [("set", t("seo.tool.goal.mode_set")), ("max", t("seo.tool.goal.mode_max"))], "set")
        + "<div class=row id=setrow>" + sp._num_input("w", t("seo.tool.1rm.weight"), "80", "0.25")
        + sp._num_input("r", t("seo.tool.1rm.reps"), "5", "1", "numeric", "12") + "</div>"
        + "<div class=row id=maxrow hidden>" + sp._num_input("m", t("seo.tool.goal.current_max"), "90", "0.25") + "</div>"
        + "<div class=row>" + sp._num_input("target", t("seo.tool.goal.target"), "100", "0.25") + sp._unit_seg(lang) + "</div>"
        + sp._seg("freq", t("seo.tool.goal.freq"), [(str(i), str(i)) for i in (1, 2, 3, 4)], "2")
        + sp._seg("level", t("seo.tool.goal.level"), [(k, t(f"seo.tool.goal.level.{k}")) for k in LEVELS], "beginner")
        + "</form>"
    )
    result = (
        f'<div class=result aria-live=polite><div class=k>{sp._e(t("seo.tool.goal.result"))}</div>'
        '<div class=big id=when>—</div><div class=note id=note></div>'
        f'<div class=table-card><table><thead><tr><th>{sp._e(t("seo.tool.goal.col_week"))}</th>'
        f'<th class=num>{sp._e(t("seo.tool.goal.col_slow"))}</th><th class=num>{sp._e(t("seo.tool.goal.col_fast"))}</th>'
        '</tr></thead><tbody id=ms></tbody></table></div>'
        f'<p class=muted>{sp._e(t("seo.tool.goal.honest"))}</p>'
        + ("" if embed else sp._log_cta(lang, "goal", "seo.tool.goal.log_cta")) + "</div>"
    )
    script = (
        f"var LANG={json.dumps(lang)},NOW={sp._js_str(t('seo.tool.goal.now', max='{max}'))},"
        f"DONEALREADY={sp._js_str(t('seo.tool.goal.already'))},LONG={sp._js_str(t('seo.tool.goal.too_long'))},"
        f"TOO={sp._js_str(t('seo.tool.1rm.too_many', max=config.E1RM_MAX_REPS))},WEEK={sp._js_str(t('seo.tool.goal.week', n='{n}'))};"
        "function month(d){return d.toLocaleDateString(LANG,{month:'long',year:'numeric'});}"
        "function addWeeks(w){var d=new Date();d.setDate(d.getDate()+7*w);return d;}"
        "document.querySelectorAll('[name=mode]').forEach(function(el){el.addEventListener('change',function(){"
        "$('setrow').hidden=this.value!=='set';$('maxrow').hidden=this.value!=='max';});});"
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,mode=val('mode'),cur=0;"
        "if(mode==='set'){var w=parseFloat($('w').value),r=parseInt($('r').value,10)||1;if(w>0){cur=e1rm(w,r,'epley',null);if(!cur){$('when').textContent='—';$('note').textContent=TOO;$('ms').innerHTML='';done(0);return;}}}"
        "else{cur=parseFloat($('m').value)||0;}"
        "var tg=parseFloat($('target').value),fr=parseInt(val('freq'),10),lv=val('level');share('','');"
        "if(!(cur>0)||!(tg>0)){$('when').textContent='—';$('note').textContent='';$('ms').innerHTML='';done(0);return;}"
        "$('note').innerHTML=NOW.replace('{max}',fu(cur,u));"
        "if(cur>=tg){$('when').textContent=DONEALREADY;$('ms').innerHTML='';done(1);return;}"
        "var a=goalWeeks(cur*k,tg*k,lv,fr,true),b=goalWeeks(cur*k,tg*k,lv,fr,false);"
        "if(a===null){$('when').textContent=LONG;$('ms').innerHTML='';done(0);return;}"
        "var d1=addWeeks(a),d2=b===null?null:addWeeks(b);"
        "$('when').textContent=d2?(month(d1)===month(d2)?month(d1):month(d1)+' — '+month(d2)):month(d1)+' — …';"
        "var fast=goalCurve(cur,lv,fr,true,C.goalMax),slow=goalCurve(cur,lv,fr,false,C.goalMax),rows=[];"
        "for(var wk=4;wk<=Math.min(C.goalMax,(b===null?a:b)+4);wk+=4){rows.push('<tr><td>'+WEEK.replace('{n}',wk)+' · '+month(addWeeks(wk))+'</td><td class=num>'"
        "+fu(slow[wk],u)+'</td><td class=num>'+fu(fast[wk],u)+'</td></tr>');if(rows.length>=12)break;}"
        "$('ms').innerHTML=rows.join('');"
        "var end=d2||d1,q='w='+encodeURIComponent(fmt(tg))+'&u='+u+'&lift='+val('lift')+'&m1='+(d1.getMonth()+1)+'&y1='+d1.getFullYear()"
        "+'&m2='+(end.getMonth()+1)+'&y2='+end.getFullYear();share('goal',q);done(1);}"
        "bind(run);"
    )
    return form, result, script


# ---------- насколько ты сильный ----------

PCT_LIFTS = ("squat", "bench", "deadlift", "total")


def tool_percentile(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    data = seo_tools.percentile_data()
    inputs = "".join(sp._num_input(f"v_{k}", t(f"seo.tool.strength-percentile.lift.{k}"), "", "0.5") for k in PCT_LIFTS)
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + sp._seg("sex", t("seo.tool.wilks.sex"), [("m", t("seo.tool.wilks.male")), ("f", t("seo.tool.wilks.female"))], "m")
        + sp._unit_seg(lang) + "</div>"
        + sp._num_input("bw", t("seo.tool.wilks.bw"), "83", "0.1")
        + f"<div class=lbl>{sp._e(t('seo.tool.strength-percentile.best'))}</div><div class=row>{inputs}</div>"
        f"<p class=muted>{sp._e(t('seo.tool.strength-percentile.raw'))}</p>"
        "</form>"
    )
    result = (
        f'<div class=result aria-live=polite><div class=k id=cls>{sp._e(t("seo.tool.strength-percentile.result"))}</div>'
        '<div class=pct-list id=out></div><div class=note id=note></div>'
        + ("" if embed else sp._log_cta(lang, "strength-percentile", "seo.tool.log_cta_total")) + "</div>"
    )
    names = {k: t(f"seo.tool.strength-percentile.lift.{k}") for k in PCT_LIFTS}
    script = (
        f"var P={json.dumps(data, separators=(',', ':'))},LANG={json.dumps(lang)},NAMES={json.dumps(names, ensure_ascii=False)},"
        f"STRONGER={sp._js_str(t('seo.tool.strength-percentile.stronger', p='{p}'))},"
        f"BELOW={sp._js_str(t('seo.tool.strength-percentile.below'))},TOP={sp._js_str(t('seo.tool.strength-percentile.top'))},"
        f"CLS={sp._js_str(t('seo.tool.strength-percentile.cls', cls='{cls}', n='{n}'))},"
        f"EMPTY={sp._js_str(t('seo.tool.strength-percentile.empty'))};"
        "var LIFTS=['squat','bench','deadlift','total'];"
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,s=val('sex'),bw=parseFloat($('bw').value)*k,v={},rows=[],best=null,cls=null,n=0;"
        "LIFTS.forEach(function(l){var x=parseFloat($('v_'+l).value);if(x>0)v[l]=x*k;});"
        "if(!v.total&&v.squat&&v.bench&&v.deadlift)v.total=v.squat+v.bench+v.deadlift;"
        "if(!(bw>0)||!Object.keys(v).length){$('out').innerHTML='';$('note').textContent=EMPTY;share('','');done(0);return;}"
        "LIFTS.forEach(function(l){if(!v[l])return;var r=percentile(P,v[l],s,bw,l);if(!r)return;cls=r[1];n=r[2];"
        "var p=Math.round(r[0]),txt=r[0]<=0?BELOW:r[0]>=100?TOP:STRONGER.replace('{p}',p);"
        "rows.push('<div class=pct><div class=pct-head><b>'+NAMES[l]+'</b><span>'+txt+'</span></div><div class=bar-track><div class=bar-fill style=\"width:'+Math.max(2,Math.min(100,r[0]))+'%\"></div></div></div>');"
        "if(!best||l==='total')best=[l,p];});"
        "$('out').innerHTML=rows.join('');$('note').textContent=cls?CLS.replace('{cls}',cls).replace('{n}',n):'';"
        "if(best){share('pct','p='+Math.max(1,Math.min(99,best[1]))+'&cls='+encodeURIComponent(cls)+'&sex='+s+'&lift='+best[0]);}"
        "done(rows.length>0);}"
        "bind(run);"
    )
    return form, result, script


TOOLS = {"tonnage": tool_tonnage, "rest-timer": tool_rest_timer, "goal": tool_goal, "strength-percentile": tool_percentile}


# ---------- карточки для сторис ----------

_INT = re.compile(r"^\d{1,4}$")
_NUM = re.compile(r"^\d{1,4}(?:\.\d{1,2})?$")


def _q(request: Request, name: str, pattern: re.Pattern, allowed: Optional[tuple] = None) -> Optional[str]:
    value = request.query_params.get(name, "")
    if allowed is not None:
        return value if value in allowed else None
    return value if pattern.match(value) else None


def goal_card_text(lang: str, weight: str, unit: str, lift: str, m1: int, y1: int, m2: int, y2: int) -> str:
    t = sp._t(lang)
    unit_label = t("seo.tool.unit_kg") if unit == "kg" else t("seo.tool.unit_lb")
    first = f"{t(f'seo.month_nom.{m1}')} {y1}"
    last = f"{t(f'seo.month_nom.{m2}')} {y2}"
    when = first if (m1, y1) == (m2, y2) else f"{first} — {last}"
    return t("seo.tool.goal.card", weight=f"{weight}\u00a0{unit_label}", lift=t(f"seo.tool.goal.card_lift.{lift}"), when=when)


def pct_card_text(lang: str, p: int, cls: str, sex: str, lift: str) -> str:
    t = sp._t(lang)
    return t("seo.tool.strength-percentile.card", p=p, cls=cls, lift=t(f"seo.tool.strength-percentile.card_lift.{lift}"),
             sex=sex)


async def card_route(request: Request) -> Response:
    lang = request.path_params.get("lang", "")
    kind = request.path_params.get("kind", "")
    if lang not in sp.LANGS or kind not in CARD_KINDS:
        return PlainTextResponse("Not found", status_code=404)
    t = sp._t(lang)
    if kind in ("rm1", "dots", "tonnage", "level"):
        made = simple_card(lang, kind, request)
        if made is None:
            return PlainTextResponse("Bad card", status_code=400)
        title, key, footer = made
    elif kind == "goal":
        weight = _q(request, "w", _NUM)
        unit = _q(request, "u", _INT, ("kg", "lb"))
        lift = _q(request, "lift", _INT, GOAL_LIFTS)
        nums = [_q(request, k, _INT) for k in ("m1", "y1", "m2", "y2")]
        if not weight or not unit or not lift or None in nums:
            return PlainTextResponse("Bad card", status_code=400)
        m1, y1, m2, y2 = (int(x) for x in nums)  # type: ignore[arg-type]
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12 and 2026 <= y1 <= 2035 and 2026 <= y2 <= 2035) or float(weight) > 1500:
            return PlainTextResponse("Bad card", status_code=400)
        weight = f"{float(weight):g}"
        title = goal_card_text(lang, weight, unit, lift, m1, y1, m2, y2)
        key = f"card-goal-{lang}-{weight}-{unit}-{lift}-{m1}-{y1}-{m2}-{y2}"
        footer = t("seo.tool.goal.card_footer", host=_host())
    else:
        p = _q(request, "p", _INT)
        cls = request.query_params.get("cls", "")
        sex = _q(request, "sex", _INT, ("m", "f"))
        lift = _q(request, "lift", _INT, PCT_LIFTS)
        if not p or not sex or not lift or not 1 <= int(p) <= 99 or cls not in seo_tools.percentile_data()["classes"][sex]:
            return PlainTextResponse("Bad card", status_code=400)
        title = pct_card_text(lang, int(p), cls, sex, lift)
        key = f"card-pct-{lang}-{p}-{cls.replace('+', 'p')}-{sex}-{lift}"
        footer = t("seo.tool.strength-percentile.card_footer", host=_host())
    data = await asyncio.to_thread(seo_static.og_png, key, title, t("bot.name"), footer)
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


CARD_KINDS = ("goal", "pct", "rm1", "dots", "tonnage", "level")


def simple_card(lang: str, kind: str, request: Request) -> Optional[tuple[str, str, str]]:
    """(текст, ключ кэша, подвал) для карточек «мой результат» простых калькуляторов."""
    t = sp._t(lang)
    footer = t("seo.tool.card_footer", host=_host())
    if kind == "level":
        level = _q(request, "l", _INT)
        lift = _q(request, "lift", _INT, ("bench", "squat", "deadlift"))
        if level is None or lift is None or not 0 <= int(level) < len(seo_tools.LEVELS):
            return None
        name = t(f"seo.strength.level.{seo_tools.LEVELS[int(level)][0]}")
        return t("seo.tool.card.level", level=name, lift=t(f"seo.tool.goal.card_lift.{lift}")), f"card-level-{lang}-{level}-{lift}", footer
    value = _q(request, "v", _NUM)
    if value is None or float(value) <= 0 or float(value) > (200000 if kind == "tonnage" else 2000):
        return None
    if kind == "dots":
        return t("seo.tool.card.dots", v=f"{float(value):g}"), f"card-dots-{lang}-{float(value):g}", footer
    unit = _q(request, "u", _INT, ("kg", "lb"))
    if unit is None:
        return None
    unit_label = t("seo.tool.unit_kg") if unit == "kg" else t("seo.tool.unit_lb")
    number = float(value)
    if kind == "tonnage" and unit == "kg" and number >= 1000:
        shown = f"{number / 1000:.1f}".rstrip("0").rstrip(".") + "\u00a0" + t("seo.tool.unit_t")
    else:
        shown = f"{number:g}\u00a0{unit_label}"
    return t(f"seo.tool.card.{kind}", v=shown), f"card-{kind}-{lang}-{number:g}-{unit}", footer


def _host() -> str:
    return urlsplit(config.site_base_url() or "").hostname or "athletediary.app"


def card_url(lang: str, kind: str, **params) -> str:
    return f"/og/card/{lang}/{kind}.png?{urlencode(params)}"


def register_routes(server) -> None:
    server.custom_route("/og/card/{lang}/{kind}.png", methods=["GET"])(card_route)
