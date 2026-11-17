import {datasets,reviews} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {chart} from '../components/common/chart.js';
import {table} from '../components/common/table.js';
import {reviewStatus} from '../components/common/status.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canWrite=()=>session.user.role!=='viewer';
const isAdmin=()=>session.user.role==='admin';
const itemLabel=v=>({pending:'待算',running:'计算中',completed:'已完成',failed:'失败'}[v]||v);

function itemEditor(rows,datasetsList){
 return '<div id="review-items">'+rows.map((r,i)=>'<div class="review-item-row" data-item-row><span class="item-index">'+(i+1)+'</span><label>数据批次<select data-dataset>'+datasetsList.map(d=>'<option value="'+d.id+'" '+(Number(r.dataset_id)===d.id?'selected':'')+'>'+esc(d.name)+'</option>').join('')+'</select></label><label>班次<select data-shift>'+['早班','晚班'].map(s=>'<option value="'+s+'" '+(r.shift===s?'selected':'')+'>'+s+'</option>').join('')+'</select></label>'+(canWrite()?'<button type="button" class="text-button" data-remove-item>移除</button>':'')+'</div>').join('')+'</div>'+(canWrite()?'<button type="button" class="button small" id="add-review-item">＋ 添加评审项</button>':'');
}

function groupForm(g,datasetsList,rule){
 const editing=!!g;
 const rows=g?g.items.map(i=>({dataset_id:i.dataset_id,shift:i.shift})):[{dataset_id:datasetsList[0]?.id,shift:'早班'},{dataset_id:datasetsList[0]?.id,shift:'晚班'}];
 return '<section class="panel review-create"><div class="section-title"><h2>'+(editing?'编辑草稿':'新建成组评审')+'</h2><span>锁定前可调整成员与误报上限</span></div>'+'<form id="review-form" data-group="'+(editing?g.id:'')+'">'+'<label>评审组名称<input name="name" required minlength="2" maxlength="160" value="'+esc(editing?g.name:'班次改善成组评审')+'" '+(canWrite()?'':'disabled')+'></label>'+'<div class="parameter-grid"><label>区间置信度<select name="confidence" '+(canWrite()?'':'disabled')+'>'+[.9,.95,.99].map(v=>'<option value="'+v+'" '+(Number(editing?g.confidence:.95)===v?'selected':'')+'>'+(v*100)+'%</option>').join('')+'</select></label><label>家族误报上限 α<select name="alpha" '+(canWrite()?'':'disabled')+'>'+(rule.alpha_options||['0.01','0.05','0.1']).map(v=>'<option value="'+v+'" '+(Math.abs(Number(editing?g.alpha:.05)-Number(v))<1e-12?'selected':'')+'>'+Number(v).toFixed(2)+'</option>').join('')+'</select></label></div>'+'<div class="section-title"><h2>评审项（批次 × 班次）</h2><span>锁定后不可增删或换样本</span></div>'+itemEditor(rows,datasetsList)+'<button type="submit" class="primary" '+(canWrite()&&datasetsList.length?'':'disabled')+'>'+(editing?'保存草稿':'创建草稿')+'</button>'+(editing&&canWrite()?' <button type="button" class="button" id="cancel-edit-review">取消编辑</button>':'')+'<p class="hint">建组只做可估计性预检，不展示任何 p 值；锁定后按整组成员独立执行，任一项失败整组保持失败，不剔除。</p></form></section>';
}

function summaryChips(list){
 const count=s=>list.filter(g=>g.status===s).length;
 const pending=list.filter(g=>['draft','locked','running'].includes(g.status)).length;
 const rows=[['待算','pending',pending],['失败','failed',count('failed')],['待发布','release',count('computed')+count('approved')],['已发布','published',count('published')]];
 const active=workspace.reviewFilter||'all';
 return '<div class="review-chips">'+rows.map(([label,key,n])=>'<button type="button" class="chip '+key+' '+(active===key?'active':'')+'" data-review-filter="'+key+'"><span>'+label+'</span><strong>'+n+'</strong></button>').join('')+'<button type="button" class="chip all '+(active==='all'?'active':'')+'" data-review-filter="all"><span>全部</span><strong>'+list.length+'</strong></button></div>';
}

function filteredGroups(list){
 const filter=workspace.reviewFilter||'all';
 if(filter==='pending')return list.filter(g=>['draft','locked','running'].includes(g.status));
 if(filter==='release')return list.filter(g=>['computed','approved'].includes(g.status));
 if(filter==='failed')return list.filter(g=>g.status==='failed');
 if(filter==='published')return list.filter(g=>g.status==='published');
 return list;
}

function groupList(list,selected){
 if(!list.length)return '<p class="empty">尚无评审组。左侧建组并锁定后开始计算。</p>';
 return '<div class="review-groups">'+list.map(g=>{const done=g.items.filter(i=>i.status==='completed').length;return '<button class="review-group '+(selected&&selected.id===g.id?'active':'')+'" data-review="'+g.id+'"><div><strong>'+esc(g.name)+'</strong>'+reviewStatus(g.status)+'</div><p>'+done+' / '+g.items.length+' 项完成 · α='+g.alpha+' · '+esc(g.confidence*100)+'% 区间</p><small>'+(g.locked_at?'锁定 '+date(g.locked_at):'创建 '+date(g.created_at))+(g.spec_fingerprint?' · 指纹 '+esc(g.spec_fingerprint.slice(0,14))+'…':'')+'</small></button>';}).join('')+'</div>';
}

function itemTable(g,showAdjusted){
 const correcting=!!g.result?.table;
 const columns=correcting?g.result.table.columns:[{key:'position',label:'项'},{key:'dataset_name',label:'数据批次'},{key:'shift',label:'班次'},{key:'status',label:'状态'},{key:'p_value',label:'原始 p 值'},{key:'p_adjusted',label:'校正 p'},{key:'error',label:'失败原因'}];
 const rows=correcting?g.result.table.rows:g.items.map(i=>({position:i.position+1,dataset_name:i.dataset_name,shift:i.shift,status:itemLabel(i.status),p_value:i.p_value??'—',p_adjusted:showAdjusted?(i.p_adjusted??'—'):'—',error:i.error||'—'}));
 let html='<div class="section-title"><h2>评审项与校正前后</h2><span>'+(correcting?'整组校正已生成':'整组未全部成功，原始 p 不得作为结论')+'</span></div>'+table(columns,rows);
 if(g.items.some(i=>i.status==='failed'))html+='<div class="review-failures">'+g.items.filter(i=>i.status==='failed').map(i=>'<p class="warning">第 '+(i.position+1)+' 项 '+esc(i.shift)+' 失败：'+esc(i.error)+'。不剔除，需恢复重试。</p>').join('')+'</div>';
 return html;
}

function detail(g){
 if(!g)return '<section class="panel empty">尚未建立评审组。先在上方建组，再锁定成员后开算。</section>';
 const r=g.result||{};
 const locked=['locked','running','computed','failed','approved','published'].includes(g.status);
 const actions=[];
 if(g.status==='draft'&&canWrite())actions.push('<button class="button" data-edit="'+g.id+'">编辑草稿</button>');
 if(g.status==='draft'&&canWrite())actions.push('<button class="primary" data-lock="'+g.id+'">锁定并开算</button>');
 if(g.status==='failed'&&canWrite())actions.push('<button class="primary" data-resume="'+g.id+'">恢复失败项（不新建）</button>');
 if(g.status==='computed'&&isAdmin())actions.push('<button class="primary" data-approve="'+g.id+'">批准整组校正结论</button>');
 if(g.status==='approved'&&isAdmin())actions.push('<button class="primary" data-publish="'+g.id+'">发布整组简报</button>');
 if(g.status==='published')actions.push('<button class="button" data-brief="'+g.id+'">下载整组简报 ↓</button>');
 if(g.status==='locked'||g.status==='running')actions.push('<span class="hint inline">后台按原成员计算待算项，可稍后刷新。</span>');
 return '<section class="panel review-detail"><div class="section-title"><h2>'+esc(g.name)+'</h2><div class="review-actions">'+reviewStatus(g.status)+actions.join('')+'</div></div>'+ (g.error?'<p class="warning">'+esc(g.error)+'</p>':'') + (g.status==='published'?'<p class="review-basis">整组依据：'+r.family_size+' 项共同校正，α='+g.alpha+'；简报含整组校正前后对照，组内原始显著性未单独分发。指纹 '+esc(g.spec_fingerprint)+'，发布 '+date(g.published_at)+'。</p>':'') + '<div class="review-meta"><span>批次 <strong>'+esc([...new Set(g.items.map(i=>i.dataset_name))].join('、'))+'</strong></span><span>成员 <strong>'+g.items.length+' 项</strong></span><span>置信度 <strong>'+esc(g.confidence*100)+'%</strong></span><span>家族误报上限 <strong>α='+g.alpha+'</strong></span><span>锁定指纹 <strong>'+esc(g.spec_fingerprint?g.spec_fingerprint.slice(0,18)+'…':'未锁定')+'</strong></span><span>锁定时间 <strong>'+(g.locked_at?date(g.locked_at):'—')+'</strong></span></div>' + (r.summary?'<div class="result-lead"><p class="eyebrow">WHOLE-GROUP CORRECTED FINDING</p><h2>'+esc(r.summary)+'</h2></div><section class="panel review-chart">'+chart(r.chart)+'<p class="hint">柱高为 p 值；校正后越过 α 线的项才可作为组内显著结论。</p></section>':'') + '<div class="panel">'+itemTable(g,g.status==='published'||g.status==='approved'||g.status==='computed')+'</div>' + (r.findings?'<div class="review-findings"><h3>整组依据</h3><ol class="questions">'+r.findings.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ol>'+(r.warnings||[]).map(v=>'<p class="warning">'+esc(v)+'</p>').join('')+'<p class="hint">规则：'+esc(workspace.meta.group_method||'')+'</p></div>':'') + '<p class="hint">独立比较仍在 <a href="#/workbench">分析工作台</a> 与 <a href="#/results">结果复核</a> 中保留；组内原始显著性不可单独批准或分发。</p></section>';
}

export async function reviewPage(id){
 const [list,datasetsList]=await Promise.all([reviews.list(),datasets.list()]);
 workspace.reviews=list;workspace.datasets=datasetsList;
 const rule=workspace.meta.group_review||{};
 let selected=id?await reviews.get(Number(id)):(workspace.reviewSelected?list.find(g=>g.id===workspace.reviewSelected):null)||list[0]||null;
 if(selected&&!list.some(g=>g.id===selected.id))list.unshift(selected);
 workspace.reviewSelected=selected?.id;
 const editing=workspace.reviewEdit?list.find(g=>g.id===workspace.reviewEdit)||null:null;
 if(editing&&editing.status!=='draft')workspace.reviewEdit=null;
 return heading('05 / FAMILY REVIEW','成组评审与 Holm 校正','把同一批班次结论当作一个检验家族。开算前锁定批次、班次与误报上限，按整组 Holm 校正判定；锁定后拒绝增删或换样本，单项失败也不剔除。','<button class="button" id="refresh-reviews">刷新评审 ↻</button>')
  + summaryChips(list)
  + '<div class="review-grid"><section>'+groupForm(editing||null,datasetsList,rule)+'<section class="panel"><div class="section-title"><h2>评审组</h2><span>'+list.length+' 组 · 待算 '+list.filter(g=>['draft','locked','running'].includes(g.status)).length+'</span></div>'+groupList(filteredGroups(list),selected)+'</section></section>'
  + detail(selected)+'</div>';
}
export function bindReviews(refresh){
 const form=document.querySelector('#review-form');
 const rowsBox=document.querySelector('#review-items');
 document.querySelector('#add-review-item')?.addEventListener('click',()=>{
   const last=document.querySelector('[data-item-row]:last-child');if(!last)return;
   if(document.querySelectorAll('[data-item-row]').length>=(workspace.meta.group_review?.max_items||20)){notify('评审项已达到上限',true);return;}
   const row=last.cloneNode(true);row.querySelectorAll('select').forEach(s=>{if(s.hasAttribute('data-dataset'))s.selectedIndex=0;else s.value=[...s.options].find(o=>o.value!==last.querySelector('[data-shift]').value)?.value||'早班';});rowsBox.append(row);renumber();bindRemove();
 });
 function renumber(){document.querySelectorAll('[data-item-row]').forEach((r,i)=>{const n=r.querySelector('.item-index');if(n)n.textContent=i+1;});}
 function bindRemove(){document.querySelectorAll('[data-remove-item]').forEach(b=>b.onclick=()=>{if(document.querySelectorAll('[data-item-row]').length<=2){notify('至少保留两个评审项',true);return;}b.closest('[data-item-row]').remove();renumber();});}
 bindRemove();
 document.querySelector('#cancel-edit-review')?.addEventListener('click',()=>{workspace.reviewEdit=null;refresh();});
 submit(form,async f=>{
   const items=[...document.querySelectorAll('[data-item-row]')].map(r=>({dataset_id:Number(r.querySelector('[data-dataset]').value),shift:r.querySelector('[data-shift]').value}));
   const payload={name:f.get('name'),confidence:Number(f.get('confidence')),alpha:Number(f.get('alpha')),items};
   const existing=form.dataset.group?Number(form.dataset.group):null;
   const g=existing?await reviews.update(existing,payload):await reviews.create(payload);
   workspace.reviewEdit=null;workspace.reviewSelected=g.id;notify(existing?'草稿已更新':'评审组草稿已创建');location.hash='#/reviews/'+g.id;refresh();
 });
 document.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>{const next='#/reviews/'+b.dataset.review;workspace.reviewSelected=Number(b.dataset.review);workspace.reviewEdit=null;if(location.hash===next)refresh();else location.hash=next;});
 document.querySelectorAll('[data-review-filter]').forEach(b=>b.onclick=()=>{workspace.reviewFilter=b.dataset.reviewFilter==='all'?null:b.dataset.reviewFilter;refresh();});
 document.querySelector('#refresh-reviews')?.addEventListener('click',refresh);
 document.querySelector('[data-edit]')?.addEventListener('click',e=>{workspace.reviewEdit=Number(e.target.dataset.edit);workspace.reviewSelected=Number(e.target.dataset.edit);refresh();});
 document.querySelector('[data-lock]')?.addEventListener('click',async e=>{
   if(!confirm('锁定后不得增删或更换样本，单项失败也不剔除。确认锁定并按整组开算？'))return;
   try{const g=await reviews.lock(Number(e.target.dataset.lock));workspace.reviewEdit=null;notify('已锁定 '+g.items.length+' 项，后台开始独立执行');await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelector('[data-resume]')?.addEventListener('click',async e=>{
   try{const g=await reviews.resume(Number(e.target.dataset.resume));notify('已按原成员恢复失败项，不重复建项');await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelector('[data-approve]')?.addEventListener('click',async e=>{
   if(!confirm('仅整组全部成功的校正结论可批准，批准后仍需发布。确认？'))return;
   try{await reviews.approve(Number(e.target.dataset.approve));notify('整组校正结论已批准，待发布');await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelector('[data-publish]')?.addEventListener('click',async e=>{
   try{await reviews.publish(Number(e.target.dataset.publish));notify('整组简报已发布');await refresh();}catch(err){notify(err.message,true);}
 });
 document.querySelector('[data-brief]')?.addEventListener('click',async e=>{
   try{await reviews.brief(Number(e.target.dataset.brief));}catch(err){notify(err.message,true);}
 });
}
