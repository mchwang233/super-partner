import React, { FormEvent, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ModelProfiles, type Model } from "./model-profiles";
import { AttachButton, AttachmentTray, TaskAttachments, useAttachments, type Attachment } from "./attachments";
import { PlanGraph, type PlanNode } from "./plan-graph";
import { ResultPage } from "./result-page";
import { MarkdownView } from "./markdown";
import { ProjectField, rememberProject } from "./project-picker";
import { canCancelMission, canDeleteMission, CancelMissionDialog, DeleteMissionButton, DeleteMissionDialog, formatUsd, MissionHistory, needsAttention, statusLabel } from "./mission-history";
import "./style.css";

const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
type Diagnosis = {id:string;trigger:string;node_id?:string|null;model_id?:string|null;status:string;report?:string|null;advice?:string|null;verdict?:"fixed"|"retry"|"quota_exhausted"|"model_unavailable"|"blocked"|null;user_action?:string|null;error?:string|null;created_at:string};
type Run = {id:string;status:string;prompt?:string;created_at?:string;updated_at?:string;diagnoses?:Diagnosis[];budget_usd:number;use_cli:boolean;project_dir?:string|null;attachments?:Attachment[];rework_rounds?:number;actual_cost_usd:number;final_result?:string;error?:string;plan:{summary:string;nodes:PlanNode[];planning_method:string;planner_model_id?:string;planning_note?:string;planning_cost_usd:number;estimate:{low_usd:number;high_usd:number}}};
type Provider = {id:string;name:string;configured:boolean};
// The task list polls often; the model catalog is large and changes rarely.
const POLL_MS = 2000, MODEL_REFRESH_EVERY = 15;

function errorText(e:unknown){return e instanceof Error?e.message:String(e)}
function canCancelRun(run:Run){return run.status!=="cancelled"&&(canCancelMission(run.status)||(run.diagnoses??[]).some(item=>item.status==="running"))}

/** Key requests touch the OS keyring; give up rather than leave the dialog spinning. */
async function keyRequest(url:string,init:RequestInit={},ms=20000){
  const controller=new AbortController(),timer=window.setTimeout(()=>controller.abort(),ms);
  try{return await fetch(url,{...init,signal:controller.signal})}
  catch(e){throw controller.signal.aborted?new Error("请求超时，请稍后重试"):e}
  finally{window.clearTimeout(timer)}
}

function ProviderCard({provider,onChanged}:{provider:Provider;onChanged:()=>void}) {
  const [key,setKey]=useState(""),[busy,setBusy]=useState<""|"save"|"test"|"remove">(""),[message,setMessage]=useState<{text:string;tone:"ok"|"warn"|"bad"}|null>(null);
  async function save(e:FormEvent){e.preventDefault();setBusy("save");setMessage(null);try{const r=await keyRequest(`${API}/api/providers/${provider.id}/key`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify({api_key:key})});const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(body.detail||`保存失败（${r.status}）`);setKey("");setMessage(body.stored==="session"?{text:"Key 已生效，但系统密钥环被锁定或不可用，只在本次运行中有效。解锁密钥环后重新保存即可长期保存。",tone:"warn"}:{text:"已保存到系统密钥环",tone:"ok"});onChanged()}catch(e){setMessage({text:errorText(e),tone:"bad"})}finally{setBusy("")}}
  async function test(){setBusy("test");setMessage(null);try{const r=await keyRequest(`${API}/api/providers/${provider.id}/test`,{method:"POST"},30000);const body=await r.json();if(!r.ok)throw new Error(body.detail||"连接失败");setMessage({text:"连接正常",tone:"ok"})}catch(e){setMessage({text:errorText(e),tone:"bad"})}finally{setBusy("")}}
  async function remove(){if(!confirm(`移除 ${provider.name} 的 API Key？`))return;setBusy("remove");setMessage(null);try{const r=await keyRequest(`${API}/api/providers/${provider.id}/key`,{method:"DELETE"});const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(body.detail||`移除失败（${r.status}）`);setMessage(body.removed==="session"?{text:"已停止使用，但系统密钥环被锁定，已保存的副本仍在其中，下次启动可能重新加载。",tone:"warn"}:{text:"已移除",tone:"ok"});onChanged()}catch(e){setMessage({text:errorText(e),tone:"bad"})}finally{setBusy("")}}
  return <article className="provider-card"><div className="provider-head"><span className={`provider ${provider.id}`}>{provider.id.slice(0,2).toUpperCase()}</span><div><b>{provider.name}</b><small className={provider.configured?"ready":"off"}>{provider.configured?"已连接":"未配置"}</small></div></div><form className="key-form" onSubmit={save}><input type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder={provider.configured?"输入新 Key 以替换":"粘贴 API Key"} minLength={8} required/><button disabled={!!busy}>{busy==="save"?"保存中…":"保存"}</button></form><div className="provider-actions"><button type="button" className="ghost" onClick={test} disabled={!provider.configured||!!busy}>{busy==="test"?"测试中…":"测试连接"}</button>{provider.configured&&<button type="button" className="danger" onClick={remove} disabled={!!busy}>{busy==="remove"?"移除中…":"移除"}</button>}</div>{message&&<p className={`provider-message ${message.tone}`} role={message.tone==="bad"?"alert":"status"}>{message.text}</p>}</article>
}

const PROJECT_KEY="super-partner.project-dir";
const HISTORY_KEY="super-partner.history";
function savedProjectDir(){try{return localStorage.getItem(PROJECT_KEY)||""}catch{return ""}}

type Theme = "light"|"dark"|"system";
const THEME_KEY="super-partner.theme";
const THEMES: {id:Theme;label:string}[] = [{id:"light",label:"浅色"},{id:"dark",label:"深色"},{id:"system",label:"跟随系统"}];
function savedTheme():Theme{try{const t=localStorage.getItem(THEME_KEY);return t==="dark"||t==="system"?t:"light"}catch{return "light"}}
function applyTheme(theme:Theme){document.documentElement.style.colorScheme=theme==="system"?"light dark":theme}

function ThemeSwitch() {
  const [theme,setTheme]=useState<Theme>(savedTheme);
  useEffect(()=>{applyTheme(theme);try{localStorage.setItem(THEME_KEY,theme)}catch{}},[theme]);
  return <div className="theme-switch" role="group" aria-label="界面主题">{THEMES.map(t=><button key={t.id} type="button" aria-pressed={theme===t.id} onClick={()=>setTheme(t.id)}>{t.label}</button>)}</div>
}

function hashTaskId(){const match=/^#task=(.+)$/.exec(location.hash);return match?decodeURIComponent(match[1]):null}

type FleetState = "ready"|"off"|"nokey"|"missing";
const FLEET_LABELS: Record<FleetState,string> = {ready:"可用",off:"已停用",nokey:"缺少 Key",missing:"未检测到"};
const FLEET_ORDER: Record<FleetState,number> = {ready:0,nokey:1,off:2,missing:3};
function fleetState(m:Model):FleetState{
  if(!m.configured)return m.channel==="cli"?"missing":"nokey";
  return m.enabled&&m.routing_ready?"ready":"off";
}
function modelPrice(m:Model){
  if(m.channel==="cli")return m.configured?`${m.auth_type==="membership"?"会员订阅":"已登录"} · 优先使用`:"本机未安装或未登录";
  if(m.provider==="mock")return "本地演示，不产生费用";
  if(m.input_per_million===null||m.output_per_million===null)return "价格未知";
  return `$${m.input_per_million} 入 · $${m.output_per_million} 出`;
}

/** Model list as a popover beside the task form's CLI switch, instead of taking a page column. */
function ModelFleet({models,onOpenAll,onOpen,onToggle,onConfigure}:{models:Model[];onOpenAll:()=>void;onOpen:(m:Model)=>void;onToggle:(m:Model)=>void;onConfigure:(m:Model)=>void}) {
  const [open,setOpen]=useState(false);
  const dockRef=useRef<HTMLDivElement>(null);
  useEffect(()=>{
    if(!open)return;
    const outside=(event:MouseEvent)=>{if(dockRef.current&&!dockRef.current.contains(event.target as globalThis.Node))setOpen(false)};
    const escape=(event:KeyboardEvent)=>{if(event.key==="Escape")setOpen(false)};
    document.addEventListener("mousedown",outside);window.addEventListener("keydown",escape);
    return()=>{document.removeEventListener("mousedown",outside);window.removeEventListener("keydown",escape)};
  },[open]);
  const fleet=models.filter(m=>m.scores!==null).sort((a,b)=>FLEET_ORDER[fleetState(a)]-FLEET_ORDER[fleetState(b)]);
  const ready=fleet.filter(m=>fleetState(m)==="ready").length;
  return <div className="fleet-anchor" ref={dockRef}>
    <button type="button" className="fleet-trigger" aria-expanded={open} aria-haspopup="dialog" onClick={()=>setOpen(!open)}><span className={`fleet-dot ${ready?"on":""}`} aria-hidden="true"/>{ready} 个模型可用<span className="fleet-chevron" aria-hidden="true">{open?"▴":"▾"}</span></button>
    {open&&<aside className="panel fleet" role="dialog" aria-label="可调度模型">
      <div className="title"><h3>可调度模型</h3><span>{ready} 个可用</span></div>
      {fleet.length===0&&<p className="empty">正在加载模型…</p>}
      <div className="fleet-list">{fleet.map(m=>{const state=fleetState(m);return <div className={`model ${state}`} key={m.id}>
        <span className={`provider ${m.provider}`}>{m.channel==="cli"?"CLI":m.provider.slice(0,2).toUpperCase()}</span>
        <button type="button" className="model-profile-link" aria-label={`查看 ${m.display_name} 的画像`} title={m.channel==="api"&&m.provider!=="mock"?"每百万 tokens 的输入 / 输出价格":undefined} onClick={()=>{setOpen(false);onOpen(m)}}><b>{m.display_name}</b><small>{modelPrice(m)}</small></button>
        <div className="model-controls">
          {state==="nokey"?<button type="button" className="model-key-button" aria-label={`配置 ${m.display_name} 的 API Key`} onClick={()=>{setOpen(false);onConfigure(m)}}>配置 Key</button>:<span className={`fleet-state ${state}`}>{FLEET_LABELS[state]}</span>}
          {state!=="missing"&&<label className="mini-switch" title={m.enabled?"停用":"启用"}><input type="checkbox" aria-label={`${m.enabled?"停用":"启用"} ${m.display_name}`} checked={m.enabled} disabled={!m.enabled&&!m.routing_ready} onChange={()=>onToggle(m)}/><span/></label>}
        </div>
      </div>})}</div>
      <button type="button" className="fleet-profiles-link" onClick={()=>{setOpen(false);onOpenAll()}}>查看全部 {models.length} 个模型画像 →</button>
    </aside>}
  </div>
}

/** Dollar amount field. Any amount above zero is valid: a step of 0.5 from a 0.01 minimum made
 * the browser reject round numbers such as 2. The text is kept separately so the field can be
 * cleared while typing. */
function BudgetInput({value,onChange,...rest}:{value:number;onChange:(value:number)=>void}&Omit<React.InputHTMLAttributes<HTMLInputElement>,"value"|"onChange">){
  const [text,setText]=useState(String(value));
  useEffect(()=>{setText(current=>+current===value?current:String(value))},[value]);
  return <input {...rest} type="number" min="0.01" step="any" inputMode="decimal" value={text} onChange={e=>{setText(e.target.value);onChange(e.target.value===""?0:+e.target.value)}}/>;
}

type Tab = "progress"|"result"|"details";
const STEPS = ["计划","确认","执行","结果"];
/** Where the task is in plan → confirm → run → result, and whether that step went wrong. */
function stepOf(status:string):{at:number;problem:boolean}{
  if(status==="awaiting_confirmation")return {at:1,problem:false};
  if(status==="succeeded")return {at:4,problem:false};
  if(status==="failed"||status==="cancelled")return {at:2,problem:true};
  return {at:2,problem:status==="paused"};
}
function defaultTab(status:string):Tab{return status==="succeeded"?"result":"progress"}
function duration(ms:number){const s=Math.max(0,Math.round(ms/1000));if(s<60)return `${s} 秒`;const m=Math.floor(s/60);return m<60?`${m} 分 ${s%60} 秒`:`${Math.floor(m/60)} 小时 ${m%60} 分`}
function elapsedOf(run:Run,now:number){
  const starts=run.plan.nodes.map(node=>node.started_at?new Date(node.started_at).getTime():NaN).filter(Number.isFinite);
  if(!starts.length)return null;
  const end=run.status==="running"||run.status==="queued"?now:new Date(run.updated_at??now).getTime();
  return duration(end-Math.min(...starts));
}

function Stepper({status}:{status:string}){
  const {at,problem}=stepOf(status);
  return <ol className="task-steps" aria-label="任务阶段">{STEPS.map((label,index)=>{
    const state=index<at||(index===at&&at===4)?"done":index===at?(problem?"problem":"current"):"todo";
    return <li key={label} className={state} aria-current={state==="current"||state==="problem"?"step":undefined}><span>{state==="done"?"✓":index+1}</span>{label}</li>;
  })}</ol>;
}

const EVENT_LABELS:Record<string,(data:Record<string,any>,title:(id:string)=>string)=>string> = {
  "plan.created":data=>`生成执行计划（${data.planning_method==="model"?`由 ${data.planner_model_id} 规划`:"规则规划"}）`,
  "run.confirmed":data=>`确认执行，预算 ${formatUsd(data.budget_usd??0)}`,
  "run.resumed":data=>`从中断处继续，预算 ${formatUsd(data.budget_usd??0)}`,
  "run.started":()=>"开始执行",
  "workspace.snapshot":data=>data.git?`记录项目起点：分支 ${data.branch}，${data.uncommitted_files} 个未提交改动`:"项目不是 git 仓库",
  "node.started":(data,title)=>`${title(data.node_id)}：开始第 ${data.attempt??1} 次执行`,
  "node.model_switched":(data,title)=>`${title(data.node_id)}：${data.model_id} ${data.reason==="model_configuration_changed"?"配置已变更":data.reason==="model_unavailable"?"服务暂时不可用":"额度用完"}，换用 ${data.next_model_id}`,
  "node.superseded":(data,title)=>`${title(data.node_id)}：上游已返工，旧结果失效，将重新执行`,
  "run.recovered":()=>"诊断已处理阻碍，任务自动继续",
  "node.succeeded":(data,title)=>`${title(data.node_id)}：完成`,
  "node.retry":(data,title)=>data.reason==="environment_fixed"?`${title(data.node_id)}：环境已修复，重新执行`:`${title(data.node_id)}：未通过，准备重试（${data.reason??""}）`,
  "node.failed":(data,title)=>`${title(data.node_id)}：失败（${data.error??""}）`,
  "run.rework":(data,title)=>`${title(data.verifier)} 发现问题，第 ${data.round} 轮返工：${data.problems??""}`,
  "approval.required":data=>`暂停：${({budget_insufficient:"预算不足以启动下一步",budget_exceeded:"花费超过预算",interrupted:"服务重启打断了执行",user_paused:"你手动暂停了任务",quota_exhausted:"模型额度用完，且没有其它可用模型",model_unavailable:"模型服务不可用，且没有其它可用模型",diagnosis_unresolved:"诊断后仍无法自动解决，请查看诊断报告"} as Record<string,string>)[data.reason]??data.reason}`,
  "run.succeeded":data=>`任务完成，共花费 ${formatUsd(data.actual_cost_usd??0)}`,
  "run.failed":data=>`任务失败：${data.error??""}`,
  "run.published":data=>`已上传到 ${data.url}`,
  "run.diagnosis_started":(data,title)=>data.trigger==="auto"?`${data.model_id??"—"} 开始排查${data.node_id?`「${title(data.node_id)}」`:""}卡住的原因，并尝试修复环境`:`开始诊断${data.node_id?`（${title(data.node_id)}）`:""}，由 ${data.model_id??"—"} 分析`,
  "run.diagnosis_fallback":data=>`${data.model_id} 无法使用（${String(data.error??"").slice(0,120)}），改由 ${data.next_model_id} 继续`,
  "run.diagnosed":data=>data.status!=="done"?"诊断失败":({fixed:"诊断：环境已修复",retry:"诊断：继续自动处理并重试",quota_exhausted:"诊断：执行模型额度耗尽，自动选择备用模型",model_unavailable:"诊断：模型服务不可用，自动选择备用模型",blocked:"诊断：需要你处理，已输出诊断报告"} as Record<string,string>)[data.verdict]??"诊断完成",
  "run.repair_shared":data=>`公共环境修复已保存，供其他项目复用：${(data.files??[]).join("、")}`,
};

/** Request, attachments, planning notes, settings, and the full event log. */
function TaskDetails({run}:{run:Run}){
  const [events,setEvents]=useState<{id:number;kind:string;data:Record<string,any>;created_at:string}[]>([]);
  useEffect(()=>{let live=true;fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}/timeline`).then(r=>r.ok?r.json():[]).then(list=>{if(live&&Array.isArray(list))setEvents(list)}).catch(()=>{});return()=>{live=false}},[run.id,run.status,run.updated_at]);
  const number=new Map(run.plan.nodes.map((node,index)=>[node.id,`${String(index+1).padStart(2,"0")} ${node.title}`]));
  const title=(id:string)=>number.get(id)??"某个步骤";
  return <div className="task-details">
    {run.prompt&&<section><h5>任务描述</h5><p className="detail-prompt">{run.prompt}</p></section>}
    <TaskAttachments apiBase={API} attachments={run.attachments}/>
    {run.plan.planning_note&&<section><h5>规划说明</h5><p className="planning-note">{run.plan.planning_note}</p></section>}
    <dl className="task-facts">
      <div><dt>规划方式</dt><dd>{run.plan.planning_method==="model"&&run.plan.planner_model_id?`由 ${run.plan.planner_model_id} 规划`:"规则规划"}</dd></div>
      <div><dt>预计花费</dt><dd>{formatUsd(run.plan.estimate.low_usd)} – {formatUsd(run.plan.estimate.high_usd)}</dd></div>
      <div><dt>预算上限</dt><dd>{formatUsd(run.budget_usd)}</dd></div>
      <div><dt>会员 CLI</dt><dd>{run.use_cli?"允许":"不使用"}</dd></div>
      <div><dt>工作位置</dt><dd>{run.project_dir?`📁 ${run.project_dir}`:"独立的临时工作区"}</dd></div>
      {run.created_at&&<div><dt>创建时间</dt><dd>{new Date(run.created_at).toLocaleString("zh-CN",{hour12:false})}</dd></div>}
    </dl>
    <section><h5>事件记录</h5>{events.length?<ol className="event-log">{events.map(event=><li key={event.id} className={event.kind.replace(".","-")}><time>{new Date(event.created_at).toLocaleTimeString("zh-CN",{hour12:false})}</time><span>{EVENT_LABELS[event.kind]?.(event.data??{},title)??event.kind}</span></li>)}</ol>:<p className="empty">还没有事件。</p>}</section>
  </div>;
}

type Quota = {model_id:string;until:string;reason:string;kind?:"quota"|"unavailable"};

/** Models out of quota: routing skips them until the reset, using backups meanwhile. */
function QuotaBanner({quota,models,onReset}:{quota:Quota[];models:Model[];onReset:(id:string)=>void}){
  return <div className="quota-banner" role="status">
    <b>模型暂不可用</b>
    {quota.map(item=><span key={item.model_id} title={item.reason}>
      {models.find(m=>m.id===item.model_id)?.display_name??item.model_id}（{item.kind==="unavailable"?"服务故障":"额度用完"}），{new Date(item.until).toLocaleString("zh-CN",{hour12:false,month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})} 后重试
      <button type="button" className="ghost" onClick={()=>onReset(item.model_id)} title="服务或额度恢复后，立即重新使用这个模型">恢复使用</button>
    </span>)}
    <small>在此之前，任务会自动换用其它可用模型。</small>
  </div>;
}

/** The latest diagnosis of a stuck task, with earlier ones a click away. */
function DiagnosisCard({run,models}:{run:Run;models:Model[]}){
  const list=run.diagnoses??[];
  const [index,setIndex]=useState(list.length-1),[open,setOpen]=useState(true);
  useEffect(()=>{setIndex(list.length-1);setOpen(true)},[list.length,run.id]);
  const item=list[Math.min(Math.max(index,0),list.length-1)];
  if(!item)return null;
  const model=models.find(m=>m.id===item.model_id)?.display_name??item.model_id??"—";
  const node=run.plan.nodes.find(n=>n.id===item.node_id);
  return <section className={`diagnosis-card ${item.status}`} aria-label="诊断报告">
    <div className="diagnosis-head">
      <b>🩺 诊断报告</b>
      <small>{item.trigger==="auto"?"自动诊断":"手动诊断"}{node?` · ${node.title}`:""} · {model} · {new Date(item.created_at).toLocaleString("zh-CN",{hour12:false})}</small>
      {list.length>1&&<span className="diagnosis-nav"><button type="button" className="ghost" disabled={index<=0} onClick={()=>setIndex(i=>i-1)} aria-label="上一次诊断">‹</button>{index+1}/{list.length}<button type="button" className="ghost" disabled={index>=list.length-1} onClick={()=>setIndex(i=>i+1)} aria-label="下一次诊断">›</button></span>}
      <button type="button" className="ghost diagnosis-fold" onClick={()=>setOpen(o=>!o)} aria-expanded={open}>{open?"收起":"展开"}</button>
    </div>
    {open&&(item.status==="running"?<p className="diagnosis-running"><i className="pulse" aria-hidden="true"/>{item.trigger==="auto"?`${model} 正在排查卡住的原因，并修复环境（安装缺失的软件、调整配置等），可能需要十几分钟…`:`正在由 ${model} 分析任务的计划、每次尝试和改动，会员 CLI 可能需要几分钟…`}</p>
      :item.status==="failed"?<p className="error">诊断没有完成：{item.error}</p>
      :<>
        {item.report&&<MarkdownView text={item.report}/>}
        {item.verdict&&<p className={`diagnosis-verdict ${item.verdict==="blocked"?"warn":"ok"}`}>{{fixed:`${model} 已修复环境，这一步会重新执行。下面是修复内容。`,retry:`系统会带着 ${model} 的诊断建议继续处理并重试。`,quota_exhausted:`${model} 确认执行模型额度耗尽，系统会自动选择备用模型接替原步骤；没有可用备用模型时才暂停。`,model_unavailable:`${model} 确认模型服务不可用，系统会自动选择备用模型，并让其他项目暂时避开故障模型。`,blocked:`${model} 判断需要你先处理，任务已暂停。${item.user_action??"下面是诊断报告。"}`}[item.verdict]}</p>}
        {item.advice&&<div className="diagnosis-advice"><b>给失败步骤的建议</b><p>{item.advice}</p>{(run.status==="failed"||run.status==="paused")&&<small>点“{run.status==="failed"?"从失败处继续":"按此预算继续"}”时，这条建议会交给失败的步骤。</small>}</div>}
      </>)}
  </section>;
}

function MissionDetail({run,models,deletingId,cancellingId,onConfirm,onResume,onCancel,onDelete,onOpenModel,onPause,onDiagnose}:{run:Run|null;models:Model[];onOpenModel:(model:Model)=>void;onPause:(run:Run)=>Promise<string|null>;onDiagnose:(run:Run)=>Promise<string|null>;deletingId:string|null;cancellingId:string|null;onConfirm:(run:Run,budget:number)=>Promise<string|null>;onResume:(run:Run,budget:number)=>Promise<string|null>;onCancel:(run:Run)=>void;onDelete:(run:Run)=>void}) {
  const [budget,setBudget]=useState(run?.budget_usd??2),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const [tab,setTab]=useState<Tab>(defaultTab(run?.status??"")),picked=useRef(false),[now,setNow]=useState(()=>Date.now());
  useEffect(()=>{setBudget(run?.budget_usd??2);setError("");picked.current=false;setTab(defaultTab(run?.status??""))},[run?.id]);
  // Follow the task's phase (e.g. jump to the result when it finishes) until the user picks a tab.
  useEffect(()=>{if(!picked.current)setTab(defaultTab(run?.status??""))},[run?.status]);
  useEffect(()=>{if(run?.status!=="running")return;const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>window.clearInterval(timer)},[run?.status]);
  if(!run)return <section className="panel detail"><div className="empty big">在左侧选择一个任务，或点右上角的“＋ 新任务”。</div></section>;
  const needsConfirm=run.status==="awaiting_confirmation"||run.status==="paused";
  const canResume=run.status==="failed"||run.status==="cancelled";
  const planner=run.plan.planning_method==="model"&&run.plan.planner_model_id?`由 ${run.plan.planner_model_id} 规划`:"规则规划";
  const nodes=run.plan.nodes,done=nodes.filter(node=>node.status==="succeeded").length;
  const stopped=nodes.filter(node=>node.status==="failed"||node.status==="cancelled"||node.status==="running");
  const spentRatio=run.budget_usd>0?Math.min(1,run.actual_cost_usd/run.budget_usd):0;
  const elapsed=elapsedOf(run,now);
  function pick(next:Tab){picked.current=true;setTab(next)}
  const canPause=run.status==="running"||run.status==="queued";
  const diagnosing=(run.diagnoses??[]).some(item=>item.status==="running");
  const canDiagnose=nodes.some(node=>(node.attempts?.length??0)>0)||run.status==="failed"||run.status==="paused";
  const stillRunning=nodes.filter(node=>node.status==="running").length;
  async function act(action:(run:Run)=>Promise<string|null>){setBusy(true);setError("");setError(await action(run!)||"");setBusy(false)}
  async function confirm(){setBusy(true);setError("");setError(await (canResume?onResume:onConfirm)(run!,budget)||"");setBusy(false)}
  return <section className="panel detail task-page">
    <div className="task-header">
      <div className="task-title">
        <p className="eyebrow">{planner}{run.project_dir&&<span className="project-path" title={run.project_dir}> · 📁 {run.project_dir}</span>}</p>
        <h3>{run.plan.summary}</h3>
      </div>
      <div className="task-actions">
        <span className={`pill ${run.status}`}>{statusLabel(run.status)}</span>
        {run.status==="succeeded"&&tab!=="result"&&<button type="button" className="primary" onClick={()=>pick("result")}>查看结果</button>}
        {canPause&&<button type="button" className="ghost" disabled={busy} onClick={()=>act(onPause)} title="不再启动新步骤；正在运行的步骤会跑完">暂停</button>}
        {canDiagnose&&<button type="button" className="ghost" disabled={busy||diagnosing} onClick={()=>act(onDiagnose)} title="排查原因；对已停止的失败步骤执行修复或切换模型，并在现有预算内继续">{diagnosing?"诊断中…":"诊断"}</button>}
        {canCancelRun(run)&&<button type="button" className="danger" disabled={cancellingId!==null} onClick={()=>onCancel(run)}>{cancellingId===run.id?"取消中…":"取消任务"}</button>}
        <DeleteMissionButton mission={run} deletingId={deletingId} onDelete={onDelete}/>
      </div>
    </div>
    <Stepper status={run.status}/>
    <div className="task-metrics">
      <div><small>进度</small><b>{done} / {nodes.length}</b><span className="spend-bar" aria-hidden="true"><i style={{width:`${nodes.length?done/nodes.length*100:0}%`}}/></span></div>
      <div><small>用时</small><b>{elapsed??"—"}</b></div>
      <div><small>花费 / 预算</small><b>{formatUsd(run.actual_cost_usd)} <span>/ {formatUsd(run.budget_usd)}</span></b><span className="spend-bar" aria-hidden="true"><i style={{width:`${spentRatio*100}%`}}/></span></div>
      <div><small>预计花费</small><b>{formatUsd(run.plan.estimate.low_usd)} – {formatUsd(run.plan.estimate.high_usd)}</b></div>
    </div>
    {needsConfirm&&<div className="action-bar">
      <p>{run.status==="paused"?(stillRunning?`任务已暂停：正在运行的 ${stillRunning} 个步骤会跑完，之后不再启动新步骤。`:run.error??"任务已暂停。确认项目状态和预算后可以继续执行。"):"确认计划和预算后，任务将自动执行，无需再次确认。"}</p>
      <label className="budget-field">预算上限 <span>$</span><BudgetInput value={budget} onChange={setBudget} aria-label="确认时的预算上限（美元）"/></label>
      <button className="primary" disabled={busy||diagnosing||cancellingId!==null||!(budget>0)} onClick={confirm}>{busy?"提交中…":run.status==="paused"?"按此预算继续":"确认并开始执行"}</button>
    </div>}
    {canResume&&<div className="action-bar resume-bar">
      <p>{run.status==="failed"?<>任务在 <b>{stopped.map(node=>`「${node.title}」`).join("、")||"某个步骤"}</b> 失败。</>:"任务已取消。"}可以从{run.status==="failed"?"失败":"中断"}的地方继续：已完成的 {done} 个步骤保留不动，其余步骤重新执行，重试次数和返工轮数重新计算。</p>
      <label className="budget-field">预算上限 <span>$</span><BudgetInput value={budget} onChange={setBudget} aria-label="继续执行的预算上限（美元）"/></label>
      <button className="primary" disabled={busy||diagnosing||deletingId!==null||!(budget>0)} onClick={confirm}>{busy?"提交中…":run.status==="failed"?"从失败处继续":"继续执行"}</button>
    </div>}
    {error&&<div className="error" role="alert">{error}</div>}
    {run.error&&<div className="error">{run.error}</div>}
    <DiagnosisCard run={run} models={models}/>
    <nav className="task-tabs" aria-label="任务内容">{([["progress","进度"],["result","结果"],["details","详情"]] as const).map(([id,label])=><button key={id} type="button" aria-pressed={tab===id} onClick={()=>pick(id)}>{label}</button>)}</nav>
    {tab==="progress"&&<PlanGraph apiBase={API} runId={run.id} runStatus={run.status} nodes={nodes} inProject={!!run.project_dir} models={models} onOpenProfile={onOpenModel} reworkRounds={run.rework_rounds}/>}
    {tab==="result"&&<ResultPage apiBase={API} run={run} embedded/>}
    {tab==="details"&&<TaskDetails run={run}/>}
  </section>
}

/** Short-lived notices when a task needs the user or has finished. */
type Toast = {id:number;runId:string;text:string;tone:"ok"|"warn"|"bad"};
const NOTICE: Record<string,{text:string;tone:Toast["tone"]}> = {
  succeeded:{text:"已完成",tone:"ok"},failed:{text:"失败了",tone:"bad"},paused:{text:"已暂停，需要你确认",tone:"warn"},
};

function App() {
  const [runs,setRuns]=useState<Run[]>([]),[models,setModels]=useState<Model[]>([]),[providerList,setProviderList]=useState<Provider[]>([]);
  const [view,setView]=useState<"missions"|"models">("missions"),[selectedModelId,setSelectedModelId]=useState("");
  const [selected,setSelected]=useState<Run|null>(null),[selectedProvider,setSelectedProvider]=useState<Provider|null>(null),[prompt,setPrompt]=useState(""),[budget,setBudget]=useState(2),[useCli,setUseCli]=useState(true),[busy,setBusy]=useState(false),[projectDir,setProjectDir]=useState(savedProjectDir);
  const [formError,setFormError]=useState(""),[offline,setOffline]=useState(false);
  const attachments=useAttachments(API),[dragging,setDragging]=useState(false);
  const [composerOpen,setComposerOpen]=useState(false),loaded=useRef(false);
  const [toasts,setToasts]=useState<Toast[]>([]),seenStatus=useRef(new Map<string,string>()),toastId=useRef(0);
  const [historyCollapsed,setHistoryCollapsed]=useState(()=>{try{return localStorage.getItem(HISTORY_KEY)==="collapsed"}catch{return false}});
  useEffect(()=>{try{localStorage.setItem(HISTORY_KEY,historyCollapsed?"collapsed":"open")}catch{}},[historyCollapsed]);
  const [deletingRunId,setDeletingRunId]=useState<string|null>(null),[deleteError,setDeleteError]=useState("");
  const [deleteTarget,setDeleteTarget]=useState<Run|null>(null);
  const [cancellingRunId,setCancellingRunId]=useState<string|null>(null),[cancelError,setCancelError]=useState("");
  const [cancelTarget,setCancelTarget]=useState<Run|null>(null);
  const loadRevision=useRef(0),pollCount=useRef(0),autoSelected=useRef(false);
  const [quota,setQuota]=useState<Quota[]>([]);
  // Separate from the main load: an older API without this route must not look offline.
  const loadQuota=async(init?:RequestInit,path="")=>{try{const r=await fetch(`${API}/api/models/quota${path}`,init);if(r.ok)setQuota(await r.json())}catch{}};
  const load=async(withModels=true)=>{
    const revision=loadRevision.current;
    void loadQuota();
    try{
      const urls=[`${API}/api/tasks`,...(withModels?[`${API}/api/models`,`${API}/api/providers`]:[])];
      const data=await Promise.all((await Promise.all(urls.map(url=>fetch(url)))).map(r=>r.json()));
      setOffline(false);
      if(revision!==loadRevision.current)return;
      setRuns(data[0] as Run[]);
      // With no tasks yet, the new-task form is the only useful thing to show.
      if(!loaded.current){loaded.current=true;if(!(data[0] as Run[]).length)setComposerOpen(true)}
      if(withModels){setModels(data[1] as Model[]);setProviderList(data[2] as Provider[])}
    }catch{setOffline(true)}
  };
  useEffect(()=>{load();const id=setInterval(()=>{pollCount.current++;load(pollCount.current%MODEL_REFRESH_EVERY===0)},POLL_MS);return()=>clearInterval(id)},[]);
  useEffect(()=>{
    // Open the newest mission once, so the workbench never starts on an empty detail panel.
    if(!autoSelected.current&&runs.length){autoSelected.current=true;const linked=runs.find(r=>r.id===hashTaskId());setSelected(current=>current??linked??runs[0]);return}
    if(selected){const updated=runs.find(r=>r.id===selected.id);if(updated)setSelected(updated)}
  },[runs]);
  // Keep the open mission in the URL so a refresh or shared link returns to it. The model page
  // is not kept there: reopening the app always starts at the task workbench.
  useEffect(()=>{
    if(!selected?.id&&!autoSelected.current)return;
    const hash=selected?`#task=${encodeURIComponent(selected.id)}`:"";
    try{history.replaceState(null,"",hash||location.pathname+location.search)}catch{}
  },[selected?.id]);
  useEffect(()=>{
    // Tell the user when a task they are not looking at finishes or needs them.
    const seen=seenStatus.current,fresh:Toast[]=[];
    for(const run of runs){
      const before=seen.get(run.id);seen.set(run.id,run.status);
      const notice=NOTICE[run.status];
      if(before&&before!==run.status&&notice)fresh.push({id:++toastId.current,runId:run.id,text:`「${run.plan.summary}」${notice.text}`,tone:notice.tone});
    }
    if(fresh.length)setToasts(current=>[...current,...fresh].slice(-4));
    const attention=runs.filter(run=>needsAttention(run.status)).length,running=runs.some(run=>run.status==="running");
    document.title=`${running?"● ":""}${attention?`(${attention}) `:""}Super Partner`;
  },[runs]);
  useEffect(()=>{if(!toasts.length)return;const timer=window.setTimeout(()=>setToasts(current=>current.slice(1)),8000);return()=>window.clearTimeout(timer)},[toasts]);
  useEffect(()=>{if(!selectedProvider)return;const close=(event:KeyboardEvent)=>{if(event.key==="Escape")setSelectedProvider(null)};window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close)},[selectedProvider]);
  async function submit(e:FormEvent){e.preventDefault();setBusy(true);setFormError("");try{const r=await fetch(`${API}/api/tasks`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({prompt,budget_usd:budget,use_cli:useCli,project_dir:projectDir.trim()||null,attachments:attachments.ids})});if(!r.ok){const body=await r.json().catch(()=>({}));throw new Error(body.detail||`生成计划失败（${r.status}）`)}try{localStorage.setItem(PROJECT_KEY,projectDir.trim())}catch{}rememberProject(projectDir.trim());const task=await r.json();autoSelected.current=true;setSelected(task);setPrompt("");attachments.clear();setComposerOpen(false);await load(false)}catch(e){setFormError(errorText(e))}finally{setBusy(false)}}
  async function confirmRun(run:Run,runBudget:number){try{const r=await fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}/confirm`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({budget_usd:runBudget})});if(!r.ok){const body=await r.json().catch(()=>({}));return body.detail||`确认失败（${r.status}）`}await load(false);return null}catch(e){return errorText(e)}}
  async function postAction(run:Run,action:"pause"|"diagnose",label:string){try{const r=await fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}/${action}`,{method:"POST"});const body=await r.json().catch(()=>({}));if(!r.ok)return body.detail||`${label}失败（${r.status}）`;loadRevision.current++;setRuns(current=>current.map(item=>item.id===run.id?body:item));setSelected(current=>current?.id===run.id?body:current);return null}catch(e){return errorText(e)}}
  async function resumeRun(run:Run,runBudget:number){try{const r=await fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}/resume`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({budget_usd:runBudget})});if(!r.ok){const body=await r.json().catch(()=>({}));return body.detail||`继续失败（${r.status}）`}await load(false);return null}catch(e){return errorText(e)}}
  function requestCancelRun(run:Run){
    if(cancellingRunId||!canCancelRun(run))return;
    setCancelError("");setCancelTarget(run);
  }
  function closeCancelDialog(){if(!cancellingRunId){setCancelTarget(null);setCancelError("")}}
  async function cancelRun(run:Run){
    if(cancellingRunId||!canCancelRun(run))return;
    setCancellingRunId(run.id);setCancelError("");
    const controller=new AbortController();
    const timeout=window.setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}/cancel`,{method:"POST",signal:controller.signal});
      if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`取消失败（${response.status}）`)}
      const updated=await response.json() as Run;
      // Ignore older polls so they cannot overwrite the confirmed cancellation.
      loadRevision.current++;
      setRuns(current=>current.map(item=>item.id===run.id?updated:item));
      setSelected(current=>current?.id===run.id?updated:current);
      setCancelTarget(null);
    }catch(error){setCancelError(controller.signal.aborted?"取消请求超时，请检查任务状态后重试。":error instanceof Error?error.message:"取消失败，请稍后重试。")}
    finally{window.clearTimeout(timeout);setCancellingRunId(null)}
  }
  function requestDeleteRun(run:Run){
    if(deletingRunId||!canDeleteMission(run.status))return;
    setDeleteError("");setDeleteTarget(run);
  }
  function closeDeleteDialog(){if(!deletingRunId){setDeleteTarget(null);setDeleteError("")}}
  async function deleteRun(run:Run){
    if(deletingRunId||!canDeleteMission(run.status))return;
    setDeletingRunId(run.id);setDeleteError("");
    const controller=new AbortController();
    const timeout=window.setTimeout(()=>controller.abort(),15000);
    try{
      const response=await fetch(`${API}/api/tasks/${encodeURIComponent(run.id)}`,{method:"DELETE",signal:controller.signal});
      if(!response.ok&&response.status!==404){const body=await response.json().catch(()=>({}));throw new Error(body.detail||`删除失败（${response.status}）`)}
      // Ignore polls started before deletion, so stale responses cannot restore the record.
      loadRevision.current++;
      setRuns(current=>current.filter(item=>item.id!==run.id));
      setSelected(current=>current?.id===run.id?null:current);
      setDeleteTarget(null);
    }catch(error){setDeleteError(controller.signal.aborted?"删除请求超时，请重试。":error instanceof Error?error.message:"删除失败，请稍后重试。")}
    finally{window.clearTimeout(timeout);setDeletingRunId(null)}
  }
  async function setModelEnabled(modelId:string,enabled:boolean){return fetch(`${API}/api/models`,{method:"PATCH",headers:{"content-type":"application/json"},body:JSON.stringify({model_id:modelId,enabled})})}
  async function toggleModel(model:Model){const enabled=!model.enabled;setModels(current=>current.map(item=>item.id===model.id?{...item,enabled}:item));try{const response=await setModelEnabled(model.id,enabled);if(!response.ok)throw new Error(await response.text())}catch(e){setModels(current=>current.map(item=>item.id===model.id?{...item,enabled:!enabled}:item));alert(`切换模型失败：${errorText(e)}`)}}
  const usedProjects=[...new Set(runs.map(run=>run.project_dir).filter((dir):dir is string=>!!dir))];
  function openModelProfile(model:Model){setSelectedModelId(model.id);setView("models");window.scrollTo({top:0})}
  function configureModel(model:Model){setSelectedProvider(providerList.find(provider=>provider.id===model.provider)||null)}
  return <main className={view==="models"?"profiles-view":undefined}>
    {quota.length>0&&<QuotaBanner quota={quota} models={models} onReset={id=>loadQuota({method:"DELETE"},`/${encodeURIComponent(id)}`)}/>}
    <header><div><span className="logo">SP</span><h1>Super Partner</h1></div><div className="header-actions"><button type="button" className="primary new-task" aria-expanded={view==="missions"&&composerOpen} onClick={()=>{setView("missions");setComposerOpen(open=>view!=="missions"||!open);window.scrollTo({top:0})}}>＋ 新任务</button><nav className="app-nav" aria-label="页面导航"><button aria-pressed={view!=="models"} onClick={()=>setView("missions")}>任务工作台</button><button aria-pressed={view==="models"} onClick={()=>setView("models")}>模型画像<span>{models.length}</span></button></nav><ThemeSwitch/></div></header>
    {offline&&<p className="offline" role="status">无法连接本地 API（{API}），正在重试…</p>}
    {view==="models"?<ModelProfiles apiBase={API} models={models} selectedId={selectedModelId} onSelect={setSelectedModelId} onToggle={toggleModel} onConfigure={configureModel}/>:<>
    <div className={`workbench ${historyCollapsed?"history-collapsed":""}`}>
    <MissionHistory runs={runs} selectedId={selected?.id} deletingId={deletingRunId} error={deleteTarget?"":deleteError} onSelect={run=>{setSelected(run)}} onDelete={requestDeleteRun} collapsed={historyCollapsed} onToggle={()=>setHistoryCollapsed(value=>!value)}/>
    <div className="workbench-main">
    {composerOpen&&<section className="composer" aria-label="新任务"><div className="composer-head"><h3>新任务</h3>{runs.length>0&&<button type="button" className="modal-close" onClick={()=>setComposerOpen(false)} aria-label="收起新任务">×</button>}</div><form onSubmit={submit} className={dragging?"dragging":undefined}
      onDragOver={e=>{if(e.dataTransfer.types.includes("Files")){e.preventDefault();setDragging(true)}}}
      onDragLeave={e=>{if(!e.currentTarget.contains(e.relatedTarget as globalThis.Node|null))setDragging(false)}}
      onDrop={e=>{if(e.dataTransfer.files.length){e.preventDefault();attachments.add([...e.dataTransfer.files])}setDragging(false)}}>
      <textarea value={prompt} onChange={e=>setPrompt(e.target.value)} onPaste={e=>{const pasted=[...e.clipboardData.files];if(pasted.length){e.preventDefault();attachments.add(pasted)}}} onKeyDown={e=>{if((e.ctrlKey||e.metaKey)&&e.key==="Enter"){e.preventDefault();e.currentTarget.form?.requestSubmit()}}} placeholder="描述你想要的结果，例如：修复登录页失败的测试、调研某个市场、实现一个新功能…" required minLength={3} aria-label="任务描述"/>
      <AttachmentTray attachments={attachments}/>
      <ProjectField apiBase={API} value={projectDir} onChange={setProjectDir} usedProjects={usedProjects}/>
      <div className="formbar"><div className="controls"><AttachButton onFiles={attachments.add}/><label className="budget-field" title="执行花费达到上限时任务会自动暂停">预算上限 <span>$</span><BudgetInput value={budget} onChange={setBudget} required/></label><label className="switch" title="关闭后本任务只使用按量计费的 API 模型"><input type="checkbox" checked={useCli} onChange={e=>setUseCli(e.target.checked)}/><span/>允许使用会员 CLI</label><ModelFleet models={models} onOpenAll={()=>setView("models")} onOpen={openModelProfile} onToggle={toggleModel} onConfigure={configureModel}/></div><div className="submit-group"><small>Ctrl + Enter</small><button className="primary" disabled={busy||attachments.uploading||!(budget>0)}>{busy?"正在生成计划…":attachments.uploading?"附件上传中…":"生成执行计划 →"}</button></div></div>
    </form>{formError&&<p className="form-error" role="alert">{formError}</p>}</section>}
      <MissionDetail run={selected} models={models} onOpenModel={openModelProfile} deletingId={deletingRunId} cancellingId={cancellingRunId} onConfirm={confirmRun} onResume={resumeRun} onPause={run=>postAction(run,"pause","暂停")} onDiagnose={run=>postAction(run,"diagnose","诊断")} onCancel={requestCancelRun} onDelete={requestDeleteRun}/>
    </div>
    </div>
    </>}
    {!!toasts.length&&<div className="toasts" role="status">{toasts.map(toast=><div key={toast.id} className={`toast ${toast.tone}`}>
      <span>{toast.text}</span>
      <button type="button" className="ghost" onClick={()=>{const run=runs.find(item=>item.id===toast.runId);if(run){setView("missions");setSelected(run)}setToasts(current=>current.filter(item=>item.id!==toast.id))}}>查看</button>
      <button type="button" className="modal-close" aria-label="关闭提示" onClick={()=>setToasts(current=>current.filter(item=>item.id!==toast.id))}>×</button>
    </div>)}</div>}
    {deleteTarget&&<DeleteMissionDialog mission={deleteTarget} pending={deletingRunId===deleteTarget.id} error={deleteError} onCancel={closeDeleteDialog} onConfirm={()=>deleteRun(deleteTarget)}/>}
    {cancelTarget&&<CancelMissionDialog mission={cancelTarget} pending={cancellingRunId===cancelTarget.id} error={cancelError} onCancel={closeCancelDialog} onConfirm={()=>cancelRun(cancelTarget)}/>}
    {selectedProvider&&<div className="modal-backdrop" onMouseDown={()=>setSelectedProvider(null)}><section className="provider-modal" role="dialog" aria-modal="true" aria-label={`配置 ${selectedProvider.name}`} onMouseDown={e=>e.stopPropagation()}><div className="modal-title"><div><p className="eyebrow">API 连接</p><h3>配置 {selectedProvider.name}</h3></div><button className="modal-close" onClick={()=>setSelectedProvider(null)} aria-label="关闭">×</button></div><p className="modal-note">Key 只会发送到本机 API，并保存在操作系统的密钥环中。</p><ProviderCard provider={selectedProvider} onChanged={async()=>{await load();const refreshed=(await (await fetch(`${API}/api/providers`)).json()) as Provider[];setSelectedProvider(refreshed.find(p=>p.id===selectedProvider.id)||null)}}/></section></div>}
  </main>
}
createRoot(document.getElementById("root")!).render(<App/>);
