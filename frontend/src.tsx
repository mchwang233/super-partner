import React, { FormEvent, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { ModelProfiles, type Model } from "./model-profiles";
import { canCancelMission, canDeleteMission, CancelMissionDialog, DeleteMissionButton, DeleteMissionDialog, MissionHistory } from "./mission-history";
import "./style.css";

const API = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
type Node = {id:string;title:string;model_id:string;status:string;estimated_cost_usd:number;needs_workspace?:boolean;result?:string};
type Run = {id:string;status:string;budget_usd:number;use_cli:boolean;project_dir?:string|null;actual_cost_usd:number;final_result?:string;error?:string;plan:{summary:string;nodes:Node[];planning_method:string;planner_model_id?:string;planning_note?:string;planning_cost_usd:number;estimate:{low_usd:number;high_usd:number}}};
type Provider = {id:string;name:string;configured:boolean};

function ProviderCard({provider,onChanged}:{provider:Provider;onChanged:()=>void}) {
  const [key,setKey]=useState(""),[busy,setBusy]=useState(false),[message,setMessage]=useState("");
  async function save(e:FormEvent){e.preventDefault();setBusy(true);setMessage("");try{const r=await fetch(`${API}/api/providers/${provider.id}/key`,{method:"PUT",headers:{"content-type":"application/json"},body:JSON.stringify({api_key:key})});if(!r.ok)throw new Error(await r.text());setKey("");setMessage("Saved securely");onChanged()}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function test(){setBusy(true);setMessage("");try{const r=await fetch(`${API}/api/providers/${provider.id}/test`,{method:"POST"});const body=await r.json();if(!r.ok)throw new Error(body.detail||"Connection failed");setMessage("Connection verified")}catch(e){setMessage(String(e))}finally{setBusy(false)}}
  async function remove(){if(!confirm(`Remove ${provider.name} API key?`))return;setBusy(true);await fetch(`${API}/api/providers/${provider.id}/key`,{method:"DELETE"});setMessage("Key removed");setBusy(false);onChanged()}
  return <article className="provider-card"><div className="provider-head"><span className={`provider ${provider.id}`}>{provider.id.slice(0,2).toUpperCase()}</span><div><b>{provider.name}</b><small className={provider.configured?"ready":"off"}>{provider.configured?"Connected":"Not configured"}</small></div></div><form className="key-form" onSubmit={save}><input type="password" autoComplete="new-password" value={key} onChange={e=>setKey(e.target.value)} placeholder={provider.configured?"Enter a replacement key":"Paste API key"} minLength={8} required/><button disabled={busy}>Save</button></form><div className="provider-actions"><button className="ghost" onClick={test} disabled={!provider.configured||busy}>Test</button>{provider.configured&&<button className="danger" onClick={remove} disabled={busy}>Remove</button>}<small>{message}</small></div></article>
}

const PROJECT_KEY="super-partner.project-dir";
function savedProjectDir(){try{return localStorage.getItem(PROJECT_KEY)||""}catch{return ""}}

type DirListing = {path:string;parent:string|null;git:boolean;roots:string[];entries:{name:string;path:string;git:boolean}[];truncated:boolean};

function DirectoryPicker({initial,onPick,onClose}:{initial:string;onPick:(path:string)=>void;onClose:()=>void}) {
  const [listing,setListing]=useState<DirListing|null>(null),[error,setError]=useState(""),[hidden,setHidden]=useState(false),[loading,setLoading]=useState(false);
  async function open(path:string|null,showHidden=hidden){setLoading(true);setError("");try{const query=new URLSearchParams();if(path)query.set("path",path);if(showHidden)query.set("hidden","true");const r=await fetch(`${API}/api/projects/browse?${query}`);const body=await r.json();if(!r.ok)throw new Error(body.detail||`Browse failed (${r.status})`);setListing(body)}catch(e){setError(e instanceof Error?e.message:String(e));if(path&&!listing)await open(null,showHidden)}finally{setLoading(false)}}
  useEffect(()=>{open(initial.trim()||null)},[]);
  useEffect(()=>{const close=(event:KeyboardEvent)=>{if(event.key==="Escape")onClose()};window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close)},[]);
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="provider-modal dir-modal" role="dialog" aria-modal="true" aria-label="Choose project folder" onMouseDown={e=>e.stopPropagation()}>
    <div className="modal-title"><div><p className="eyebrow">PROJECT FOLDER</p><h3>Choose a project</h3></div><button className="modal-close" onClick={onClose} aria-label="Close">×</button></div>
    <div className="dir-bar"><button className="ghost" onClick={()=>listing?.parent&&open(listing.parent)} disabled={!listing?.parent||loading} title={listing&&!listing.parent?"Already at the top of the allowed folders":"Parent folder"}>↑ Up</button><code className="dir-path" title={listing?.path}>{listing?.path||"…"}</code>{listing?.git&&<span className="git-badge">git</span>}</div>
    {listing&&!listing.parent&&<p className="dir-hint">Top of the allowed folders{listing.roots.length>1?` (${listing.roots.join(", ")})`:""}. Set SUPER_PARTNER_PROJECT_ROOTS to allow others.</p>}
    {error&&<p className="dir-error">{error}</p>}
    <div className="dir-list">{listing&&listing.entries.length===0&&<p className="empty">No subfolders.</p>}{listing?.entries.map(entry=><button key={entry.path} className="dir-entry" onClick={()=>open(entry.path)} onDoubleClick={()=>onPick(entry.path)} title="Click to open, double-click to select"><span>📁 {entry.name}</span>{entry.git&&<span className="git-badge">git</span>}</button>)}{listing?.truncated&&<p className="empty">Showing the first {listing.entries.length} folders.</p>}</div>
    <div className="dir-footer"><label className="dir-hidden"><input type="checkbox" checked={hidden} onChange={e=>{setHidden(e.target.checked);open(listing?.path||null,e.target.checked)}}/> Show hidden</label><button onClick={()=>listing&&onPick(listing.path)} disabled={!listing||loading}>Use this folder</button></div>
  </section></div>
}

function NativePickButton({onPick}:{onPick:(path:string)=>void}) {
  const [status,setStatus]=useState<{available:boolean;reason:string|null}|null>(null),[waiting,setWaiting]=useState(false);
  useEffect(()=>{fetch(`${API}/api/projects/native-picker`).then(r=>r.json()).then(setStatus).catch(()=>setStatus(null))},[]);
  if(!status?.available)return null;
  async function pick(){setWaiting(true);try{const r=await fetch(`${API}/api/projects/native-picker`,{method:"POST"});const body=await r.json();if(!r.ok)throw new Error(body.detail||`Folder dialog failed (${r.status})`);if(body.path)onPick(body.path)}catch(e){alert(e instanceof Error?e.message:String(e))}finally{setWaiting(false)}}
  return <button type="button" className="ghost project-browse" onClick={pick} disabled={waiting} title="Opens the folder dialog on the desktop running Super Partner">{waiting?"Waiting for dialog…":"System dialog…"}</button>
}

function App() {
  const [runs,setRuns]=useState<Run[]>([]),[models,setModels]=useState<Model[]>([]),[providerList,setProviderList]=useState<Provider[]>([]);
  const [view,setView]=useState<"missions"|"models">("missions"),[selectedModelId,setSelectedModelId]=useState("");
  const [selected,setSelected]=useState<Run|null>(null),[selectedProvider,setSelectedProvider]=useState<Provider|null>(null),[prompt,setPrompt]=useState(""),[budget,setBudget]=useState(2),[useCli,setUseCli]=useState(true),[busy,setBusy]=useState(false),[projectDir,setProjectDir]=useState(savedProjectDir),[pickingDir,setPickingDir]=useState(false);
  const [deletingRunId,setDeletingRunId]=useState<string|null>(null),[deleteError,setDeleteError]=useState("");
  const [deleteTarget,setDeleteTarget]=useState<Run|null>(null);
  const [cancellingRunId,setCancellingRunId]=useState<string|null>(null),[cancelError,setCancelError]=useState("");
  const [cancelTarget,setCancelTarget]=useState<Run|null>(null);
  const loadRevision=useRef(0);
  const load=async()=>{
    const revision=loadRevision.current;
    const [r,m,p]=await Promise.all([fetch(`${API}/api/tasks`),fetch(`${API}/api/models`),fetch(`${API}/api/providers`)]);
    const [runData,modelData,providerData]=await Promise.all([r.json() as Promise<Run[]>,m.json() as Promise<Model[]>,p.json() as Promise<Provider[]>]);
    if(revision!==loadRevision.current)return;
    setRuns(runData);setModels(modelData);setUseCli(modelData.some(model=>model.channel==="cli"&&model.enabled));setProviderList(providerData);
  };
  useEffect(()=>{load();const id=setInterval(load,2000);return()=>clearInterval(id)},[]);
  useEffect(()=>{if(selected){const updated=runs.find(r=>r.id===selected.id);if(updated)setSelected(updated)}},[runs]);
  useEffect(()=>{if(!selectedProvider)return;const close=(event:KeyboardEvent)=>{if(event.key==="Escape")setSelectedProvider(null)};window.addEventListener("keydown",close);return()=>window.removeEventListener("keydown",close)},[selectedProvider]);
  async function submit(e:FormEvent){e.preventDefault();setBusy(true);try{const r=await fetch(`${API}/api/tasks`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({prompt,budget_usd:budget,use_cli:useCli,project_dir:projectDir.trim()||null})});if(!r.ok){const body=await r.json().catch(()=>({}));throw new Error(body.detail||`Planning failed (${r.status})`)}try{localStorage.setItem(PROJECT_KEY,projectDir.trim())}catch{}const task=await r.json();setSelected(task);setPrompt("");await load()}catch(e){alert(String(e))}finally{setBusy(false)}}
  async function confirmRun(){if(!selected)return;const r=await fetch(`${API}/api/tasks/${selected.id}/confirm`,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({budget_usd:budget})});if(!r.ok){const body=await r.json().catch(()=>({}));alert(body.detail||`Confirm failed (${r.status})`)}await load()}
  function requestCancelRun(run:Run){
    if(cancellingRunId||!canCancelMission(run.status))return;
    setCancelError("");setCancelTarget(run);
  }
  function closeCancelDialog(){if(!cancellingRunId){setCancelTarget(null);setCancelError("")}}
  async function cancelRun(run:Run){
    if(cancellingRunId||!canCancelMission(run.status))return;
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
  async function toggleModel(model:Model){const enabled=!model.enabled;setModels(current=>current.map(item=>item.id===model.id?{...item,enabled}:item));try{const response=await setModelEnabled(model.id,enabled);if(!response.ok)throw new Error(await response.text())}catch(e){setModels(current=>current.map(item=>item.id===model.id?{...item,enabled:!enabled}:item));alert(`Model switch failed: ${String(e)}`)}}
  async function toggleAllCli(enabled:boolean){const previous=models;setUseCli(enabled);setModels(current=>current.map(model=>model.channel==="cli"?{...model,enabled}:model));try{const cliModels=models.filter(model=>model.channel==="cli");const responses=await Promise.all(cliModels.map(model=>setModelEnabled(model.id,enabled)));const failed=responses.findIndex(response=>!response.ok);if(failed>=0)throw new Error(`${cliModels[failed].display_name}: ${await responses[failed].text()}`)}catch(e){setUseCli(!enabled);setModels(previous);alert(`CLI switch failed: ${String(e)}`)}}
  function openModelProfile(model:Model){setSelectedModelId(model.id);setView("models");window.scrollTo({top:0})}
  function configureModel(model:Model){setSelectedProvider(providerList.find(provider=>provider.id===model.provider)||null)}
  return <main className={view==="models"?"profiles-view":undefined}>
    <header><div><span className="logo">SP</span><h1>Super Partner</h1></div><nav className="app-nav" aria-label="页面导航"><button aria-pressed={view==="missions"} onClick={()=>setView("missions")}>任务工作台</button><button aria-pressed={view==="models"} onClick={()=>setView("models")}>模型画像<span>{models.length}</span></button></nav><span className="tag">local · autonomous · cost-aware</span></header>
    {view==="models"?<ModelProfiles apiBase={API} models={models} selectedId={selectedModelId} onSelect={setSelectedModelId} onToggle={toggleModel} onConfigure={configureModel}/>:<>
    <section className="hero"><p className="eyebrow">MULTI-MODEL ORCHESTRATOR</p><h2>Describe the outcome.<br/><em>We assemble the team.</em></h2><form onSubmit={submit}><textarea value={prompt} onChange={e=>setPrompt(e.target.value)} placeholder="Fix a bug, research a market, build a feature…" required minLength={3}/><div className="project-row"><input className="project-input" value={projectDir} onChange={e=>setProjectDir(e.target.value)} placeholder="Project directory (optional) — CLI agents edit it in place" spellCheck={false}/>{projectDir&&<button type="button" className="ghost project-clear" onClick={()=>setProjectDir("")} aria-label="Clear project directory">×</button>}<NativePickButton onPick={setProjectDir}/><button type="button" className="ghost project-browse" onClick={()=>setPickingDir(true)}>Browse…</button></div><div className="formbar"><div className="controls"><label>Hard limit <b>${budget.toFixed(2)}</b><input type="range" min="0.1" max="20" step="0.1" value={budget} onChange={e=>setBudget(+e.target.value)}/></label><label className="switch"><input type="checkbox" checked={useCli} onChange={e=>toggleAllCli(e.target.checked)}/><span/>Use membership CLI</label></div><button disabled={busy}>{busy?"Planning…":"Build execution plan →"}</button></div></form></section>
    <div className="grid"><MissionHistory runs={runs} selectedId={selected?.id} deletingId={deletingRunId} error={deleteTarget?"":deleteError} onSelect={setSelected} onDelete={requestDeleteRun}/>
      <section className="panel detail">{selected?<><div className="title"><div><p className="eyebrow">EXECUTION GRAPH · CLI {selected.use_cli?"ON":"OFF"}</p>{selected.project_dir&&<p className="project-path" title={selected.project_dir}>{selected.project_dir}</p>}<h3>{selected.plan.summary}</h3></div><span className={`pill ${selected.status}`}>{selected.status.replaceAll("_"," ")}</span></div><div className="estimate"><div><small>Expected</small><b>${selected.plan.estimate.low_usd.toFixed(4)}–${selected.plan.estimate.high_usd.toFixed(4)}</b></div><div><small>Hard limit</small><b>${selected.budget_usd.toFixed(2)}</b></div><div><small>Actual</small><b>${selected.actual_cost_usd.toFixed(6)}</b></div></div><div className="nodes">{selected.plan.nodes.map((n,i)=><article key={n.id}><span className="num">{String(i+1).padStart(2,"0")}</span><div><b>{n.title}</b><small>{n.model_id}{n.needs_workspace&&selected.project_dir?" · edits project":""} · ${n.estimated_cost_usd.toFixed(6)}</small>{n.result&&<p>{n.result}</p>}</div><span className={`state ${n.status}`}>{n.status}</span></article>)}</div>{(selected.status==="awaiting_confirmation"||selected.status==="paused")&&<button className="confirm" disabled={cancellingRunId!==null} onClick={confirmRun}>{selected.status==="paused"?"Resume with current limit":"Confirm plan & run autonomously"}</button>}{canCancelMission(selected.status)&&<button type="button" className="confirm danger" disabled={cancellingRunId!==null} onClick={()=>requestCancelRun(selected)}>{cancellingRunId===selected.id?"取消中…":"Cancel mission"}</button>}<DeleteMissionButton mission={selected} deletingId={deletingRunId} onDelete={requestDeleteRun}/>{selected.final_result&&<div className="result"><small>FINAL RESULT</small><p>{selected.final_result}</p></div>}{selected.error&&<div className="error">{selected.error}</div>}</>:<div className="empty big">Select a mission to inspect its plan.</div>}</section>
      <aside className="panel"><div className="title"><h3>Model fleet</h3><span>{models.filter(m=>m.configured&&m.enabled&&m.routing_ready).length} active</span></div><button className="fleet-profiles-link" onClick={()=>setView("models")}>查看所有模型画像 · {models.length} 个 →</button>{models.filter(m=>m.scores!==null).map(m=><div className={`model ${m.enabled?"":"model-disabled"}`} key={m.id}><span className={`provider ${m.provider}`}>{m.channel==="cli"?"CLI":m.provider.slice(0,2).toUpperCase()}</span><button className="model-profile-link" aria-label={`查看 ${m.display_name} 的画像`} onClick={()=>openModelProfile(m)}><b>{m.display_name}</b><small>{m.channel==="cli"?(m.configured?`${m.auth_type} · preferred`:"not detected"):`$${m.input_per_million} in · $${m.output_per_million} out`}</small></button><div className="model-controls"><span className={m.configured?"ready":"off"}>{m.configured?"ready":m.channel==="cli"?"off":"key"}</span>{m.channel==="api"&&m.provider!=="mock"&&<button className="model-key-button" aria-label={`配置 ${m.display_name} 的 API Key`} onClick={()=>configureModel(m)}>配置</button>}<label className="mini-switch" title={m.enabled?"Disable model":"Enable model"}><input type="checkbox" aria-label={`${m.enabled?"停用":"启用"} ${m.display_name}`} checked={m.enabled} disabled={!m.enabled&&!m.routing_ready} onChange={()=>toggleModel(m)}/><span/></label></div></div>)}</aside>
    </div>
    </>}
    {deleteTarget&&<DeleteMissionDialog mission={deleteTarget} pending={deletingRunId===deleteTarget.id} error={deleteError} onCancel={closeDeleteDialog} onConfirm={()=>deleteRun(deleteTarget)}/>}
    {cancelTarget&&<CancelMissionDialog mission={cancelTarget} pending={cancellingRunId===cancelTarget.id} error={cancelError} onCancel={closeCancelDialog} onConfirm={()=>cancelRun(cancelTarget)}/>}
    {pickingDir&&<DirectoryPicker initial={projectDir} onClose={()=>setPickingDir(false)} onPick={path=>{setProjectDir(path);setPickingDir(false)}}/>}
    {selectedProvider&&<div className="modal-backdrop" onMouseDown={()=>setSelectedProvider(null)}><section className="provider-modal" role="dialog" aria-modal="true" aria-label={`Configure ${selectedProvider.name}`} onMouseDown={e=>e.stopPropagation()}><div className="modal-title"><div><p className="eyebrow">API CONNECTION</p><h3>Configure {selectedProvider.name}</h3></div><button className="modal-close" onClick={()=>setSelectedProvider(null)} aria-label="Close">×</button></div><p className="modal-note">The key is sent only to your local API and stored in the operating-system keyring.</p><ProviderCard provider={selectedProvider} onChanged={async()=>{await load();const refreshed=(await (await fetch(`${API}/api/providers`)).json()) as Provider[];setSelectedProvider(refreshed.find(p=>p.id===selectedProvider.id)||null)}}/></section></div>}
  </main>
}
createRoot(document.getElementById("root")!).render(<App/>);
