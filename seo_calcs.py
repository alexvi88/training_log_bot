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
- Белок и калории (`seo_tools.protein_range`, `calorie_targets`): граммы на
  день и на приём, поддержка и коридор под цель. Кнопка под результатом зовёт
  взвешиваться и писать тренировки — дневника еды в продукте нет.

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
# Остальные языки сайта в каталоге — формы как у английского (one/other).
_PLURAL_DEFAULT = {"one": 1, "other": 2}


def _tonnage_objects(lang: str) -> list[dict]:
    """Предметы бота с формами слова по категориям множественного числа."""
    out = []
    for emoji, kg, obj_id in formatting._TONNAGE_OBJECTS:
        forms = {cat: i18n.t_in(lang, f"tonnage.object.{obj_id}", n=n) for cat, n in _PLURAL_SAMPLES.get(lang, _PLURAL_DEFAULT).items()}
        out.append({"e": emoji, "kg": kg, "f": forms})
    return out


# Сравнение «это как N предметов» — тем же правилом, что
# formatting.format_tonnage_equivalent (seed 0: самый лёгкий подходящий
# предмет); тест гоняет эту функцию в node против бота.
TONNAGE_JS = (
        "function equiv(total,u){var kg=u==='lb'?total/LBK:total;if(kg<150)return '';var c=[];"
        "OBJ.forEach(function(o){var n=Math.round(kg/o.kg);if(n>=2&&n<=40)c.push([o,n]);});"
        "if(!c.length){var f=OBJ.filter(function(o){return o.kg<=kg;});if(!f.length)return '';var o=f[f.length-1];c=[[o,Math.max(1,Math.round(kg/o.kg))]];}"
        "var o=c[0][0],n=c[0][1],cat=PR.select(n),noun=o.f[cat]||o.f.other;"
        "return EQ.replace('{count}',n).replace('{noun}',noun).replace('{emoji}','').trim();}"
)


def tonnage_prelude(lang: str) -> str:
    """Данные для TONNAGE_JS: предметы с формами слова, строка «это как…», единицы."""
    return (f"var OBJ={json.dumps(_tonnage_objects(lang), ensure_ascii=False)},PR=new Intl.PluralRules({json.dumps(lang)}),"
            f"EQ={sp._js_str(i18n.t_in(lang, 'tonnage.equivalent', count='{count}', noun='{noun}', emoji='{emoji}'))},"
            f"TON={sp._js_str(i18n.t_in(lang, 'seo.tool.unit_t'))},LBK=C.lbPerKg;")


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
        + ("" if embed else sp._log_cta(lang, "tonnage", "seo.tool.tonnage.log_cta")) + "</div>"
    )
    script = (
        tonnage_prelude(lang) + TONNAGE_JS +
        "function tonText(total,u){if(u==='kg'&&total>=1000)return fmt(total/1000)+'<span class=u>\u00a0'+TON+'</span>';return fu(total,u);}"
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
        '<div class="big big-words" id=when>—</div><div class=note id=note></div>'
        f'<div class=table-card><table><thead><tr><th>{sp._e(t("seo.tool.goal.col_week"))}</th>'
        f'<th class=num>{sp._e(t("seo.tool.goal.col_slow"))}</th><th class=num>{sp._e(t("seo.tool.goal.col_fast"))}</th>'
        '</tr></thead><tbody id=ms></tbody></table></div>'
        f'<p class=muted>{sp._e(t("seo.tool.goal.honest"))}</p>'
        + ("" if embed else sp._log_cta(lang, "goal", "seo.tool.goal.log_cta")) + "</div>"
    )
    script = (
        f"var NOW={sp._js_str(t('seo.tool.goal.now', max='{max}'))},"
        f"DONEALREADY={sp._js_str(t('seo.tool.goal.already'))},LONG={sp._js_str(t('seo.tool.goal.too_long'))},"
        f"TOO={sp._js_str(t('seo.tool.1rm.too_many', max=config.E1RM_MAX_REPS))},WEEK={sp._js_str(t('seo.tool.goal.week', n='{n}'))};"
        # Месяц и год — из каталога, а не Intl: русский Intl дописывает «г.»
        # («ноябрь 2026 г.»), а на карточке для сторис те же слова без него.
        f"var MN={json.dumps([t(f'seo.month_nom.{m}') for m in range(1, 13)], ensure_ascii=False)};"
        "function month(d){return MN[d.getMonth()]+'\u00a0'+d.getFullYear();}"
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


# Пример при открытии: одно движение, чтобы ответ был виден сразу.
PCT_EXAMPLE = {"bench": "100"}


def tool_percentile(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    data = seo_tools.percentile_data()
    inputs = "".join(sp._num_input(f"v_{k}", t(f"seo.tool.strength-percentile.lift.{k}"), PCT_EXAMPLE.get(k, ""), "0.5")
                     for k in PCT_LIFTS)
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
        f'<p class=muted id=ctx>{sp._e(t("seo.tool.strength-percentile.context"))}</p><p class=muted id=cooper hidden></p>'
        + ("" if embed else sp._log_cta(lang, "strength-percentile", "seo.tool.log_cta_total")) + "</div>"
    )
    names = {k: t(f"seo.tool.strength-percentile.lift.{k}") for k in PCT_LIFTS}
    script = (
        f"var P={json.dumps(data, separators=(',', ':'))},LANG={json.dumps(lang)},NAMES={json.dumps(names, ensure_ascii=False)},"
        f"STRONGER={sp._js_str(t('seo.tool.strength-percentile.stronger', p='{p}'))},"
        f"BELOW={sp._js_str(t('seo.tool.strength-percentile.below'))},TOP={sp._js_str(t('seo.tool.strength-percentile.top'))},"
        f"CLS={sp._js_str(t('seo.tool.strength-percentile.cls', cls='{cls}', n='{n}'))},"
        f"EMPTY={sp._js_str(t('seo.tool.strength-percentile.empty'))},"
        f"COOPER={sp._js_str(t('seo.tool.strength-percentile.cooper', r='{r}'))};"
        "var LIFTS=['squat','bench','deadlift','total'];"
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,s=val('sex'),bw=parseFloat($('bw').value)*k,v={},rows=[],best=null,cls=null,n=0;"
        "LIFTS.forEach(function(l){var x=parseFloat($('v_'+l).value);if(x>0)v[l]=x*k;});"
        "if(!v.total&&v.squat&&v.bench&&v.deadlift)v.total=v.squat+v.bench+v.deadlift;"
        "if(!(bw>0)||!Object.keys(v).length){$('out').innerHTML='';$('note').textContent=EMPTY;$('cooper').hidden=true;share('','');done(0);return;}"
        "LIFTS.forEach(function(l){if(!v[l])return;var r=percentile(P,v[l],s,bw,l);if(!r)return;cls=r[1];n=r[2];"
        "var p=Math.round(r[0]),txt=r[0]<=0?BELOW:r[0]>=100?TOP:STRONGER.replace('{p}',p);"
        "rows.push('<div class=pct><div class=pct-head><b>'+NAMES[l]+'</b><span>'+txt+'</span></div><div class=bar-track><div class=bar-fill style=\"width:'+Math.max(2,Math.min(100,r[0]))+'%\"></div></div></div>');"
        "if(!best||l==='total')best=[l,p];});"
        "var cp=$('cooper');if(v.bench&&bw>0){cp.textContent=COOPER.replace('{r}',String(Math.round(v.bench/bw*100)/100));cp.hidden=false;}else{cp.hidden=true;}"
        "$('out').innerHTML=rows.join('');$('note').textContent=cls?CLS.replace('{cls}',cls).replace('{n}',n):'';"
        "if(best){share('pct','p='+Math.max(1,Math.min(99,best[1]))+'&cls='+encodeURIComponent(cls)+'&sex='+s+'&lift='+best[0]);}"
        "done(rows.length>0);}"
        "bind(run);"
    )
    return form, result, script


# ---------- 5/3/1 ----------


def tool_531(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        + sp._seg("mode", t("seo.tool.goal.mode"), [("set", t("seo.tool.goal.mode_set")), ("max", t("seo.tool.goal.mode_max"))], "set")
        + "<div class=row id=setrow>" + sp._num_input("w", t("seo.tool.1rm.weight"), "100", "0.25")
        + sp._num_input("r", t("seo.tool.1rm.reps"), "5", "1", "numeric", "12") + "</div>"
        + "<div class=row id=maxrow hidden>" + sp._num_input("m", t("seo.tool.goal.current_max"), "115", "0.25") + "</div>"
        + "<div class=row>" + sp._seg("tm", t("seo.tool.531.tm"), [("90", "90%"), ("85", "85%")], "90")
        + sp._seg("kind", t("seo.tool.531.kind"), [("upper", t("seo.tool.531.upper")), ("lower", t("seo.tool.531.lower"))], "upper")
        + "</div>" + sp._unit_seg(lang) + "</form>"
    )
    kg = t("seo.tool.unit_kg")
    plan = seo_tools.five31(seo_tools.e1rm(100, 5), seo_tools.WARMUP_STEP["kg"])
    names = [t(f"seo.tool.531.week.{i}") for i in range(1, 5)]
    rows = "".join(f"<tr><th scope=row>{sp._e(names[i])}</th>" + "".join(
        f"<td class=num>{sp._w(w, kg)}<small>× {reps} · {round(p * 100)}%</small></td>" for p, w, reps in week) + "</tr>"
        for i, week in enumerate(plan["weeks"]))
    result = (
        '<div class=result aria-live=polite><div class=pair>'
        f'<div><div class=k>{sp._e(t("seo.tool.1rm.result"))}</div><div class=big id=orm>{sp._w(seo_tools.e1rm(100, 5), kg)}</div></div>'
        f'<div><div class=k>{sp._e(t("seo.tool.531.tm_result"))}</div><div class=big id=tmv>{sp._w(plan["tm"], kg)}</div></div></div>'
        '<div class=note id=note></div>'
        f'<div class="table-card five31"><table><thead><tr><th>{sp._e(t("seo.tool.531.col.week"))}</th>'
        f'<th class=num>{sp._e(t("seo.tool.531.col.set", n=1))}</th><th class=num>{sp._e(t("seo.tool.531.col.set", n=2))}</th>'
        f'<th class=num>{sp._e(t("seo.tool.531.col.set", n=3))}</th></tr></thead><tbody id=weeks>{rows}</tbody></table></div>'
        f'<p class=muted id=wu></p><p class=muted id=next></p>'
        + ("" if embed else sp._log_cta(lang, "531", "seo.tool.531.log_cta")) + "</div>"
    )
    script = (
        f"var WEEKS={json.dumps(names, ensure_ascii=False)},WU={sp._js_str(t('seo.tool.531.warmup', sets='{sets}'))},"
        f"NEXT={sp._js_str(t('seo.tool.531.next', add='{add}', tm='{tm}'))},TOO={sp._js_str(t('seo.tool.1rm.too_many', max=config.E1RM_MAX_REPS))},"
        f"STEP={json.dumps(seo_tools.WARMUP_STEP)},UP={json.dumps(seo_tools.FIVE31_STEP_UP)};"
        "document.querySelectorAll('[name=mode]').forEach(function(el){el.addEventListener('change',function(){"
        "$('setrow').hidden=this.value!=='set';$('maxrow').hidden=this.value!=='max';});});"
        "function cell(x,u){return fu(x[1],u)+'<small>× '+x[2]+' · '+Math.round(x[0]*100)+'%</small>';}"
        "function run(){var u=val('unit'),orm=0;$('note').textContent='';"
        "if(val('mode')==='set'){var w=parseFloat($('w').value),r=parseInt($('r').value,10)||1;if(w>0){orm=e1rm(w,r,'epley',null);"
        "if(!orm){$('orm').textContent=$('tmv').textContent='—';$('note').textContent=TOO;$('weeks').innerHTML='';done(0);return;}}}"
        "else{orm=parseFloat($('m').value)||0;}"
        "if(!(orm>0)){$('orm').textContent=$('tmv').textContent='—';$('weeks').innerHTML='';$('wu').textContent=$('next').textContent='';done(0);return;}"
        "var p=five31(orm,STEP[u],parseInt(val('tm'),10)/100);$('orm').innerHTML=fu(orm,u);$('tmv').innerHTML=fu(p.tm,u);"
        "$('weeks').innerHTML=p.weeks.map(function(wk,i){return '<tr><th scope=row>'+WEEKS[i]+'</th>'+wk.map(function(x){return '<td class=num>'+cell(x,u)+'</td>';}).join('')+'</tr>';}).join('');"
        "$('wu').innerHTML=WU.replace('{sets}',p.warmup.map(function(x){return fu(x[0],u)+' × '+x[1];}).join(', '));"
        "var add=UP[u][val('kind')];$('next').innerHTML=NEXT.replace('{add}',fu(add,u)).replace('{tm}',fu(p.tm+add,u));done(1);}"
        "bind(run);"
    )
    return form, result, script


# ---------- подходы на мышцу в неделю ----------


def volume_catalog(lang: str) -> dict[str, dict[str, float]]:
    """Имя упражнения каталога на языке страницы (без регистра и «ё») →
    {мышца: доля подхода} — правилом бота (seo_catalog.share)."""
    import seed_data
    import seo_catalog
    import seo_library
    import seo_plans

    out: dict[str, dict[str, float]] = {}
    for _g, identity in seed_data.EXERCISE_TEMPLATES:
        if seed_data.is_catalog_hidden(identity):
            continue
        shares = {}
        for url in seo_plans.VOLUME_MUSCLES:
            group, target = seo_library._MUSCLE_BY_URL[url]
            value = seo_catalog.share(identity, group, target)
            if value:
                shares[url] = value
        if shares:
            out[normalize_name(seed_data.localized_exercise_name(identity, lang))] = shares
    return out


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.casefold().replace(chr(0x451), chr(0x435))).strip()


def volume_example(lang: str) -> str:
    """Пример плана — «Всё тело — 3 дня» из бота, по строке на упражнение."""
    import seed_data

    lines = []
    for _day, exercises in seed_data.PROGRAM_BY_KEY["fullbody3"]["days"]:
        for identity, scheme in exercises:
            sets = re.match(r"\d+", scheme)
            lines.append(f"{seed_data.localized_exercise_name(identity, lang)} {sets.group(0) if sets else 3}")
    return "\n".join(lines)


def volume_limits() -> dict:
    """Минимумы и потолок недельного объёма — те же, что тренер держит в программе
    (ai_trainer.PROGRAM_*), по адресам мышц сайта."""
    import ai_trainer
    import seo_library

    targets = {"side_delts": "side-delts", "quads": "quads", "hamstrings": "hamstrings", "glutes": "glutes"}
    groups = {group: url for url, group, target in seo_library.MUSCLES if target is None and url in ("biceps", "triceps")}
    mins = {targets[k]: v for k, v in ai_trainer.PROGRAM_TARGET_MIN_SETS.items() if k in targets}
    mins.update({groups[k]: v for k, v in ai_trainer.PROGRAM_GROUP_MIN_SETS.items() if k in groups})
    return {"min": mins, "max": ai_trainer.PROGRAM_MAX_WEEKLY_SETS}


# Разбор плана строками и счёт — тот же, что plan_volume ниже (тест гоняет
# обе стороны на одних строках). Число в конце строки — подходы; схема
# «4×8–12» — тоже подходы (первое число); без числа — 3 подхода.
VOLUME_JS = (
    "function norm(s){return s.toLowerCase().replace(/\\u0451/g,'\\u0435').replace(/\\s+/g,' ').trim();}"
    "function parse(line){var m=line.match(/^(.*?)[\\s:—–-]*(\\d{1,2})(?:\\s*[x×\\u0445*]\\s*[\\d–-]+)?\\s*$/i);"
    "if(m&&m[1].trim())return [norm(m[1]),parseInt(m[2],10)];return line.trim()?[norm(line),3]:null;}"
    "function weekly(text,times){var tot={},bad=[];text.split(/\\n/).forEach(function(line){var p=parse(line);if(!p)return;var sh=CAT[p[0]];"
    "if(!sh){bad.push(line.trim());return;}Object.keys(sh).forEach(function(k){tot[k]=(tot[k]||0)+p[1]*sh[k]*times;});});return [tot,bad];}"
)
_PLAN_LINE = re.compile(r"^(.*?)[\s:—–-]*(\d{1,2})(?:\s*[x×\u0445*]\s*[\d–-]+)?\s*$", re.I)


def plan_volume(text: str, lang: str, times: int = 1) -> tuple[dict[str, float], list[str]]:
    """Python-двойник VOLUME_JS: ({мышца: подходов в неделю}, [нераспознанные строки])."""
    catalog = volume_catalog(lang)
    totals: dict[str, float] = {}
    bad: list[str] = []
    for line in text.split("\n"):
        match = _PLAN_LINE.match(line)
        if match and match.group(1).strip():
            name, sets = normalize_name(match.group(1)), int(match.group(2))
        elif line.strip():
            name, sets = normalize_name(line), 3
        else:
            continue
        shares = catalog.get(name)
        if shares is None:
            bad.append(line.strip())
            continue
        for muscle, value in shares.items():
            totals[muscle] = totals.get(muscle, 0.0) + sets * value * times
    return totals, bad


def tool_weekly_volume(lang: str, embed: bool = False) -> tuple[str, str, str]:
    import seo_library
    import seo_plans

    t = sp._t(lang)
    example = volume_example(lang)
    form = (
        "<form class=calc onsubmit='return false'>"
        f"<label>{sp._e(t('seo.tool.weekly-volume.plan'))}<textarea id=plan rows=12 spellcheck=false "
        f"autocomplete=off>{sp._e(example)}</textarea><small class=hint>{sp._e(t('seo.tool.weekly-volume.hint'))}</small></label>"
        + sp._seg("times", t("seo.tool.weekly-volume.times"), [("1", "×1"), ("2", "×2")], "1")
        + "</form>"
    )
    rows = "".join(f'<tr data-m="{url}"><th scope=row>{sp._e(seo_library.muscle_name(url, lang))}</th><td class=num>—</td><td></td></tr>'
                   for url in seo_plans.VOLUME_MUSCLES)
    result = (
        f'<div class=result aria-live=polite><div class=k>{sp._e(t("seo.tool.weekly-volume.result"))}</div>'
        f'<div class=table-card><table><thead><tr><th>{sp._e(t("seo.split.col.muscle"))}</th>'
        f'<th class=num>{sp._e(t("seo.split.col.sets_week"))}</th><th>{sp._e(t("seo.tool.weekly-volume.col.note"))}</th></tr></thead>'
        f'<tbody id=vol>{rows}</tbody></table></div><div class=note id=note></div>'
        + ("" if embed else sp._log_cta(lang, "weekly-volume", "seo.tool.weekly-volume.log_cta")) + "</div>"
    )
    script = (
        f"var CAT={json.dumps(volume_catalog(lang), ensure_ascii=False, separators=(',', ':'))},LIM={json.dumps(volume_limits())},"
        f"LOW={sp._js_str(t('seo.tool.weekly-volume.low', min='{min}'))},HIGH={sp._js_str(t('seo.tool.weekly-volume.high', max='{max}'))},"
        f"UNKNOWN={sp._js_str(t('seo.tool.weekly-volume.unknown', names='{names}'))},EMPTY={sp._js_str(t('seo.tool.weekly-volume.empty'))};"
        + VOLUME_JS +
        "function run(){var r=weekly($('plan').value,parseInt(val('times'),10)||1),tot=r[0],any=false;"
        "document.querySelectorAll('#vol tr').forEach(function(tr){var k=tr.dataset.m,v=tot[k]||0,c=tr.querySelectorAll('td');"
        "c[0].textContent=v?fmt(v):'0';if(v)any=true;var mn=LIM.min[k],note='';"
        "if(mn&&v>0&&v<mn)note=LOW.replace('{min}',mn);if(v>LIM.max)note=HIGH.replace('{max}',LIM.max);c[1].textContent=note;});"
        "$('note').textContent=r[1].length?UNKNOWN.replace('{names}',r[1].slice(0,5).join(', ')):(any?'':EMPTY);done(any);}"
        "$('plan').addEventListener('input',run);bind(run);"
    )
    return form, result, script


# ---------- белок и калории ----------
#
# Еду дневник не ведёт — кнопка под результатом зовёт взвешиваться и писать
# тренировки, а не «записать рацион». Граммы и ккал — целыми (ккал — до
# десятка): точнее формула всё равно не знает.

# Общие для двух калькуляторов функции страницы: диапазон с единицей и
# пересчёт веса тела при смене кг ↔ lb.
NUTRITION_JS = (
    "function rg(lo,hi,u,step){step=step||1;var a=Math.round(lo/step)*step,b=Math.round(hi/step)*step;"
    "return (a===b?String(a):a+'–'+b)+'<span class=u> '+u+'</span>';}"
    "function swapUnit(id){document.querySelectorAll('[name=unit]').forEach(function(el){el.addEventListener('change',function(){"
    "var x=parseFloat($(id).value);if(x>0)$(id).value=String(Math.round(this.value==='lb'?x*C.lbPerKg:x/C.lbPerKg));});});}"
)


def _range(lo: float, hi: float, unit: str, step: int = 1) -> str:
    """То же, что `rg` в NUTRITION_JS: «128–176 г» с неразрывным пробелом."""
    a, b = (int(seo_tools.round_to(x, step)) for x in (lo, hi))
    return f"{a if a == b else f'{a}–{b}'}<span class=u> {sp._e(unit)}</span>"


def tool_protein(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    goals = [(g, t(f"seo.tool.protein.goal.{g}")) for g in seo_tools.PROTEIN_GOALS]
    form = (
        "<form class=calc onsubmit='return false'>"
        + sp._seg("goal", t("seo.tool.protein.goal"), goals, "gain")
        + "<div class=row>" + sp._num_input("bw", t("seo.tool.wilks.bw"), "80", "0.1") + sp._unit_seg(lang) + "</div>"
        + sp._seg("meals", t("seo.tool.protein.meals"), [(str(n), str(n)) for n in seo_tools.PROTEIN_MEALS], "4")
        + "</form>"
    )
    g = t("seo.tool.unit_g")
    lo, hi = seo_tools.protein_range(80, "gain")
    rate = seo_tools.PROTEIN_G_PER_KG["gain"]
    result = (
        '<div class=result aria-live=polite><div class=pair>'
        f'<div><div class=k>{sp._e(t("seo.tool.protein.result"))}</div><div class=big id=day>{_range(lo, hi, g)}</div></div>'
        f'<div><div class=k>{sp._e(t("seo.tool.protein.per_meal"))}</div><div class=big id=meal>{_range(lo / 4, hi / 4, g)}</div></div></div>'
        f'<div class=note id=note>{sp._e(t("seo.tool.protein.rate_kg", lo=sp._num(rate[0]), hi=sp._num(rate[1])))}</div>'
        f'<p class=muted>{sp._e(t("seo.tool.protein.honest"))}</p>'
        + ("" if embed else sp._log_cta(lang, "protein", "seo.tool.protein.log_cta")) + "</div>"
    )
    script = (
        f"var G={sp._js_str(g)},RKG={sp._js_str(t('seo.tool.protein.rate_kg', lo='{lo}', hi='{hi}'))},"
        f"RLB={sp._js_str(t('seo.tool.protein.rate_lb', lo='{lo}', hi='{hi}'))};"
        + NUTRITION_JS +
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,bw=parseFloat($('bw').value)*k,goal=val('goal'),n=parseInt(val('meals'),10)||4;"
        "if(!(bw>0)){$('day').textContent=$('meal').textContent='—';$('note').textContent='';done(0);return;}"
        "var r=proteinRange(bw,goal),pk=C.proteinPerKg[goal];$('day').innerHTML=rg(r[0],r[1],G);$('meal').innerHTML=rg(r[0]/n,r[1]/n,G);"
        "$('note').textContent=u==='lb'?RLB.replace('{lo}',(pk[0]/C.lbPerKg).toFixed(2)).replace('{hi}',(pk[1]/C.lbPerKg).toFixed(2))"
        ":RKG.replace('{lo}',fmt(pk[0])).replace('{hi}',fmt(pk[1]));done(1);}"
        "swapUnit('bw');bind(run);"
    )
    return form, result, script


def tool_calories(lang: str, embed: bool = False) -> tuple[str, str, str]:
    t = sp._t(lang)
    acts = "".join(f'<option value="{a}"{" selected" if a == "moderate" else ""}>{sp._e(t(f"seo.tool.calories.act.{a}"))}</option>'
                   for a in seo_tools.ACTIVITY)
    goals = [(g, t(f"seo.tool.calories.goal.{g}")) for g in seo_tools.CALORIE_GOALS]
    form = (
        "<form class=calc onsubmit='return false'>"
        "<div class=row>" + sp._seg("sex", t("seo.tool.wilks.sex"), [("m", t("seo.tool.wilks.male")), ("f", t("seo.tool.wilks.female"))], "m")
        + sp._unit_seg(lang) + "</div>"
        "<div class=row>" + sp._num_input("age", t("seo.tool.calories.age"), "30", "1", "numeric")
        + sp._num_input("bw", t("seo.tool.wilks.bw"), "80", "0.1") + "</div>"
        "<div class=row id=cmrow>" + sp._num_input("hcm", t("seo.tool.calories.height_cm"), "180", "1") + "</div>"
        "<div class=row id=inrow hidden>" + sp._num_input("hft", t("seo.tool.calories.height_ft"), "5", "1", "numeric")
        + sp._num_input("hin", t("seo.tool.calories.height_in"), "11", "0.5") + "</div>"
        f"<label>{sp._e(t('seo.tool.calories.activity'))}<select id=act>{acts}</select></label>"
        + sp._seg("goal", t("seo.tool.calories.goal"), goals, "maintain")
        + "</form>"
    )
    kcal = t("seo.tool.unit_kcal")
    c = seo_tools.calorie_targets(80, 180, 30, "m", "moderate", "maintain")
    result = (
        '<div class=result aria-live=polite><div class=pair>'
        f'<div><div class=k>{sp._e(t("seo.tool.calories.keep"))}</div><div class=big id=keep>{_range(c["maintenance"], c["maintenance"], kcal, 10)}</div></div>'
        f'<div><div class=k>{sp._e(t("seo.tool.calories.target"))}</div><div class=big id=tgt>{_range(c["lo"], c["hi"], kcal, 10)}</div></div></div>'
        f'<div class=note id=note>{t("seo.tool.calories.bmr", bmr=_range(c["bmr"], c["bmr"], kcal, 10))}</div>'
        f'<p class=muted>{sp._e(t("seo.tool.calories.honest"))}</p>'
        + ("" if embed else sp._log_cta(lang, "calories", "seo.tool.calories.log_cta")) + "</div>"
    )
    script = (
        f"var KC={sp._js_str(kcal)},BMR={sp._js_str(t('seo.tool.calories.bmr', bmr='{bmr}'))},"
        f"AGE={sp._js_str(t('seo.tool.calories.age_range', lo=seo_tools.CALORIE_AGE[0], hi=seo_tools.CALORIE_AGE[1]))};"
        + NUTRITION_JS +
        "document.querySelectorAll('[name=unit]').forEach(function(el){el.addEventListener('change',function(){"
        "$('cmrow').hidden=this.value!=='kg';$('inrow').hidden=this.value!=='lb';});});"
        "function height(u){if(u==='kg')return parseFloat($('hcm').value);"
        "var ft=parseFloat($('hft').value)||0,inch=parseFloat($('hin').value)||0;return (ft*12+inch)*C.cmPerIn;}"
        "function run(){var u=val('unit'),k=u==='lb'?1/C.lbPerKg:1,w=parseFloat($('bw').value)*k,h=height(u),a=parseFloat($('age').value);"
        "var keep=$('keep'),tgt=$('tgt'),note=$('note');"
        "if(!(w>0)||!(h>0)||!(a>0)){keep.textContent=tgt.textContent='—';note.textContent='';done(0);return;}"
        "if(a<C.calorieAge[0]||a>C.calorieAge[1]){keep.textContent=tgt.textContent='—';note.textContent=AGE;done(0);return;}"
        "var c=calorieTargets(w,h,a,val('sex'),$('act').value,val('goal'));"
        "keep.innerHTML=rg(c.maintenance,c.maintenance,KC,10);tgt.innerHTML=rg(c.lo,c.hi,KC,10);"
        "note.innerHTML=BMR.replace('{bmr}',rg(c.bmr,c.bmr,KC,10));done(1);}"
        "swapUnit('bw');bind(run);"
    )
    return form, result, script


TOOLS = {"tonnage": tool_tonnage, "rest-timer": tool_rest_timer, "goal": tool_goal, "strength-percentile": tool_percentile,
         "531": tool_531, "weekly-volume": tool_weekly_volume, "protein": tool_protein, "calories": tool_calories}


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
    return t("seo.tool.goal.share", weight=f"{weight}\u00a0{unit_label}", lift=t(f"seo.tool.goal.card_lift.{lift}"), when=when)


def pct_card_text(lang: str, p: int, cls: str, sex: str, lift: str) -> str:
    t = sp._t(lang)
    return t("seo.tool.strength-percentile.share", p=p, cls=cls, lift=t(f"seo.tool.strength-percentile.card_lift.{lift}"),
             sex=sex)


async def card_route(request: Request) -> Response:
    # Адрес собирает скрипт страницы из `<html lang>` (pt-BR, pt-PT) — код тоже годится.
    lang = sp.lang_from_code(request.path_params.get("lang", ""))
    kind = request.path_params.get("kind", "")
    if lang is None or kind not in CARD_KINDS:
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
        lift = _q(request, "lift", _INT, ("bench", "squat", "deadlift", "total"))
        if level is None or lift is None or not 0 <= int(level) < len(seo_tools.LEVELS):
            return None
        name = t(f"seo.strength.level.{seo_tools.LEVELS[int(level)][0]}")
        lift_name = t(f"seo.tool.goal.card_lift.{lift}")
        lifted, reps, bw = _q(request, "w", _NUM), _q(request, "r", _INT), _q(request, "bw", _NUM)
        unit = _q(request, "u", _INT, ("kg", "lb"))
        if lifted and bw and unit and 0 < float(lifted) <= 1500 and 0 < float(bw) <= 700 and 1 <= int(reps or 1) <= 30:
            # С весом и результатом — то, чем хвастаются; старые ссылки без них рисуют прежнюю карточку.
            unit_label = t("seo.tool.unit_kg") if unit == "kg" else t("seo.tool.unit_lb")
            weight = f"{float(lifted):g}\u00a0{unit_label}" + (f"\u00a0×\u00a0{int(reps)}" if reps and int(reps) > 1 else "")
            text = t("seo.tool.card.level_full", lift=lift_name[:1].upper() + lift_name[1:], weight=weight,
                     bw=f"{float(bw):g}\u00a0{unit_label}", level=name)
            return text, f"card-level-{lang}-{level}-{lift}-{float(lifted):g}-{int(reps or 1)}-{float(bw):g}-{unit}", footer
        return t("seo.tool.card.level", level=name, lift=lift_name), f"card-level-{lang}-{level}-{lift}", footer
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
    return f"/og/card/{sp.lang_segment(lang)}/{kind}.png?{urlencode(params)}"


def register_routes(server) -> None:
    server.custom_route("/og/card/{lang}/{kind}.png", methods=["GET"])(card_route)
