import {heading} from '../components/common/header.js';
import {table} from '../components/common/table.js';
import {esc,time} from '../utils/format.js';
import {RECON_STATUS,DIMENSION_LABELS} from '../constants/enums.js';
import {store} from '../stores/business.js';
import {download} from '../api/client.js';

const badge=s=>`<span class="status ${esc(s)}">${esc(RECON_STATUS[s]||s)}</span>`;

function resultPanel(job){
  if(job.status!=='completed'||!job.result)return '';
  const r=job.result,basis=job.frozen_basis||{};
  const dimRows=r.by_department.map(x=>({
    部门:x.department,
    用量变动:x['用量变动'],因子变动:x['因子变动'],部门归属变动:x['部门归属变动'],
    部门合计:x.total,
  }));
  return `<section class="report recon-result">
    <h2>归因贡献（完整对账通过后展示）</h2>
    <div class="metrics">
      <div><small>基准期总排放 kgCO₂e</small><strong>${esc(r.original_base_total)}</strong></div>
      <div><small>对比期总排放 kgCO₂e</small><strong>${esc(r.original_current_total)}</strong></div>
      <div><small>原报告差额</small><strong>${esc(r.report_delta)}</strong></div>
      <div><small>贡献加总</small><strong>${esc(r.components_total)}</strong></div>
    </div>
    <p class="method">${esc(r.method)}</p>
    <p class="${r.tie_out_ok?'ok':'error'}">对账结论：${r.tie_out_ok?'加总等于原报告差额，对账闭合 ✓':'加总不一致，禁止发布 ✗'}</p>
    <div class="report-actions"><button class="recon-export" data-id="${job.id}">下载归因 CSV</button></div>
    ${table(dimRows)}
    <details><summary>按部门×维度的贡献明细（${r.components.length} 项）</summary>
      ${table(r.components.map(x=>({部门:x.department,维度:DIMENSION_LABELS[x.dimension]||x.dimension,贡献kgCO2e:x.value})))}
    </details>
    <h3>冻结依据</h3>
    <div class="frozen-basis">
      <p>配对规则：${esc(basis.pairing_rule||'')}</p>
      <p>分配规则：${esc(basis.allocation_rule||'')}</p>
      <p>基准报告 #${esc(basis.base_run_id)} ${esc(basis.base_run_name||'')} · SHA256 <code>${esc(basis.base_checksum||job.base_checksum)}</code></p>
      <p>对比报告 #${esc(basis.current_run_id)} ${esc(basis.current_run_name||'')} · SHA256 <code>${esc(basis.current_checksum||job.current_checksum)}</code></p>
      <p>口径版本：基准 ${esc(basis.base_policy_version||'—')} → 对比 ${esc(basis.current_policy_version||'—')}</p>
      <p>冻结行数：基准 ${esc(basis.base_line_count??'—')} / 对比 ${esc(basis.current_line_count??'—')}；
         单边缺项：基准侧 ${esc(r.missing_base)} 行、对比侧 ${esc(r.missing_current)} 行（零用量并借用有记录侧因子与部门）</p>
    </div>
  </section>`;
}

function jobCard(job,c){
  const canReset=store.session?.role==='admin'&&(job.status==='failed'||job.status==='rejected');
  const progress=job.total_pairs?Math.min(100,Math.round((job.cursor_no-1)/job.total_pairs*100)):null;
  return `<article class="recon-job">
    <div class="recon-head"><h3>任务 #${job.id}：报告 ${job.base_run_id} → ${job.current_run_id}</h3>${badge(job.status)}</div>
    <p class="muted">发起：${esc(job.created_by)} · ${time(job.created_at)} · 批大小 ${esc(job.batch_size)} · 代次 epoch ${job.epoch}</p>
    ${job.status==='running'&&progress?`<div class="progress"><i style="width:${progress}%"></i></div><p class="muted">已处理 ${job.cursor_no-1}/${job.total_pairs||'?'} 行，中断后自动续跑</p>`:''}
    ${job.reject_message?`<p class="error">拒算/失败原因（${esc(job.reject_code||'error')}）：${esc(job.reject_message)}</p>`:''}
    ${resultPanel(job)}
    ${(job.attempts?.length)?`<details><summary>执行者与代次（${job.attempts.length} 次尝试，旧代次写入会被 fence 拒绝）</summary>
      ${table(job.attempts.map(a=>({尝试:a.attempt_no,代次epoch:a.epoch,执行者:a.worker_id,开始:time(a.started_at),结束:a.ended_at?time(a.ended_at):'—',结果:a.outcome||'进行中'})))}
    </details>`:''}
    ${canReset?`<button class="recon-reset" data-id="${job.id}">重置并重新排队</button>`:''}
  </article>`;
}

export function reconciliation(c,d){
  const role=store.session?.role;
  const approved=d.runs.filter(r=>r.approved&&r.status==='completed');
  const options=approved.map(r=>`<option value="${r.id}">#${r.id} ${esc(r.name)}（${esc(r.snapshot.policy.version)}）</option>`).join('');
  const noApproved=approved.length<2?'<p class="empty">至少需要两份已复核报告才能发起两期归因，请先在“核算与复核”完成复核。</p>':'';
  return heading('VARIANCE ATTRIBUTION / 05','两期差异归因','选两份已复核报告按冻结连续编号配对；用量/因子/部门归属三维度平均边际贡献，加总等于原报告差额。')
  +(role==='viewer'?'':(approved.length<2?noApproved:`<form id="recon-form" class="inline-form">
      <label>基准期报告<select name="base_run_id">${options}</select></label>
      <label>对比期报告<select name="current_run_id">${options}</select></label>
      <label>批大小（行/批）<input name="batch_size" type="number" min="1" max="1000000" value="50000"></label>
      <button>发起归因</button>
    </form><p class="muted">前置校验：缺编号、重号、单位不可比将拒算并说明原因；单边缺项按零用量处理。</p>`))
  +`<h2>归因任务</h2>`
  +(d.reconciliations.length?d.reconciliations.map(j=>jobCard(j,c)).join('')
    :'<p class="empty">暂无归因任务。先在“核算与复核”完成两份报告的复核，再回到本页发起。</p>');
}
