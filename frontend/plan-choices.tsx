import React, { useEffect, useState } from "react";

// Scope decisions the lead made while planning (TaskPlan.choices). Before confirming, the user
// picks an answer for each; an answer other than the lead's sends the plan back to be made again
// with the answers (POST /api/tasks/{id}/choices), in place.
export type PlanChoice = { question: string; assumed: string; alternative?: string };

function Option({ checked, onPick, children }: { checked: boolean; onPick: () => void; children: React.ReactNode }) {
  return <button type="button" role="radio" aria-checked={checked} className={`choice-option${checked ? " picked" : ""}`} onClick={onPick}>
    <i aria-hidden="true" />{children}
  </button>;
}

export function PlanChoices({ choices, editable, onReplan }: {
  choices: PlanChoice[]; editable: boolean; onReplan: (picks: string[]) => Promise<string | null>;
}) {
  const [picks, setPicks] = useState<string[]>(() => choices.map(() => "assumed"));
  const [own, setOwn] = useState<string[]>(() => choices.map(() => ""));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const key = choices.map(choice => choice.question).join("\n");
  useEffect(() => { setPicks(choices.map(() => "assumed")); setOwn(choices.map(() => "")); setError(""); }, [key]);
  const answers = picks.map((pick, index) => pick === "own" ? own[index].trim() : pick);
  const changed = picks.some(pick => pick !== "assumed");
  const ready = answers.every(answer => answer);
  function pick(index: number, value: string) { setPicks(current => current.map((item, at) => at === index ? value : item)); }
  async function replan() {
    setBusy(true); setError("");
    setError(await onReplan(answers) || "");
    setBusy(false);
  }
  return <section className="plan-choices" aria-label="规划时的取舍">
    <div className="plan-choices-head">
      <p className="eyebrow">规划时的取舍</p>
      <span>{editable ? `${choices.length} 项 · 默认按负责人的建议，改选后可重新规划` : `${choices.length} 项`}</span>
    </div>
    <ol>{choices.map((choice, index) => <li key={index}>
      <p className="choice-question"><span>{String(index + 1).padStart(2, "0")}</span>{choice.question}</p>
      {editable ? <div className="choice-options" role="radiogroup" aria-label={choice.question}>
        <Option checked={picks[index] === "assumed"} onPick={() => pick(index, "assumed")}>
          <span>{choice.assumed}<em className="choice-tag">负责人建议</em></span>
        </Option>
        {choice.alternative && <Option checked={picks[index] === "alternative"} onPick={() => pick(index, "alternative")}>
          <span>{choice.alternative}</span>
        </Option>}
        {picks[index] !== "own" && <button type="button" role="radio" aria-checked={false} className="choice-own-toggle"
          onClick={() => pick(index, "own")}>＋ 写其他要求</button>}
        {picks[index] === "own" && <input className="choice-own" type="text" autoFocus value={own[index]} placeholder="例如：两主两从，固定优先级"
          aria-label={`${choice.question} 的其他要求`}
          onChange={event => { const value = event.target.value; setOwn(current => current.map((item, at) => at === index ? value : item)); }} />}
      </div> : <p className="choice-answer">{choice.assumed}</p>}
    </li>)}</ol>
    {editable && changed && <div className="choice-actions">
      <span>改了选择：负责人会按你的选择重新规划，约 5 分钟，完成前先别确认。</span>
      <button type="button" className="primary" disabled={busy || !ready} onClick={replan}>{busy ? "重新规划中…" : "按我的选择重新规划"}</button>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
