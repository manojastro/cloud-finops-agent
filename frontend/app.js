const money = value => `$${Number(value).toFixed(2)}`;
const headers = {'Content-Type':'application/json','X-Actor':'portfolio-reviewer','X-Role':'approver'};
let currentRecommendationId = null;

async function load() {
  const response = await fetch('/api/v1/dashboard');
  if (!response.ok) throw new Error('Dashboard API unavailable');
  const data = await response.json();
  currentRecommendationId = data.recommendation_id;
  const plan = data.rca.recommended_actions[0].parameters;
  document.querySelector('#mode').textContent = data.mode;
  document.querySelector('#spend').textContent = money(data.cloud_spend_usd);
  document.querySelector('#validated').textContent = money(data.validated_savings_usd);
  document.querySelector('#realized').textContent = money(data.realized_savings_usd);
  document.querySelector('#risk').textContent = `${data.risk_score} / 100`;
  document.querySelector('#unsafe-saving').textContent = `${money(data.potential_savings_usd)} calculated monthly savings`;
  document.querySelector('#safe-saving').textContent = `${money(data.validated_savings_usd)} calculated monthly savings`;
  document.querySelector('#decision-status').textContent = data.recommendation_status.replaceAll('_',' ');
  document.querySelector('#decision-title').textContent = plan.label;
  document.querySelector('#peak-label').textContent = plan.peak_day ? `Day ${plan.peak_day} peak` : 'Observed peak';
  document.querySelector('#peak-note').textContent = plan.recurring_peak
    ? `Settlement load repeats on day ${plan.peak_day} for eight hours. The typed plan, 31-day replay, and Terraform schedule are bound to that day.`
    : 'Historical peak days drift. Fixed monthly scheduling is blocked and the workflow retains safe capacity.';
  document.querySelector('#terraform').textContent = data.terraform_proposal;
  document.querySelector('#validation-status').textContent = data.validation.passed ? 'ALL PASS' : 'BLOCKED';
  const attestation = data.approval_attestation;
  document.querySelector('#attestation').textContent = attestation?.status === 'BOUND'
    ? `Bound approval · ${attestation.reviewer} (${attestation.role}) · plan ${attestation.plan_sha256.slice(0,12)}… · Terraform ${attestation.terraform_sha256.slice(0,12)}…`
    : data.recommendation_status === 'REAPPROVAL_REQUIRED'
      ? 'Legacy approval is unbound. A new reviewer decision is required before simulation.'
      : 'Approval attestation: pending reviewer decision.';
  const metrics = data.validation.metrics.map(item => `
    <div class="metric"><strong>${item.name.replaceAll('_',' ')}</strong><span>✓ ${item.projected_value} ${item.unit}</span><small>Guardrail ${item.comparison === 'lte' ? '≤' : '≥'} ${item.threshold} ${item.unit}</small></div>`).join('');
  const alignment = Object.entries(data.validation.alignment_checks).map(([name, passed]) => `
    <div class="metric"><strong>${name.replaceAll('_',' ')}</strong><span>${passed ? '✓ BOUND' : '✕ FAIL'}</span><small>Exact plan / replay / artifact alignment</small></div>`).join('');
  document.querySelector('#guardrails').innerHTML = metrics + alignment;
  document.querySelector('#evidence').innerHTML = data.rca.evidence.map(item => `
    <article class="evidence-item"><span>${item.kind}</span><h3>${item.claim}</h3><p>${item.observed_value}${item.unit ? ` ${item.unit}` : ''} · ${item.source}</p></article>`).join('');
  document.querySelector('#approve-btn').disabled = !['AWAITING_APPROVAL','REAPPROVAL_REQUIRED'].includes(data.recommendation_status);
  document.querySelector('#apply-btn').disabled = data.recommendation_status !== 'APPROVED';
}

async function post(path, body) {
  const response = await fetch(path,{method:'POST',headers,body: body ? JSON.stringify(body) : undefined});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || 'Request failed');
  return payload;
}

document.querySelector('#approve-btn').addEventListener('click', async () => {
  const message = document.querySelector('#action-message');
  try {
    await post(`/api/v1/recommendations/${currentRecommendationId}/approval`,{decision:'APPROVE',reason:'Shadow replay and exact artifact alignment passed every guardrail.'});
    message.textContent='Approval recorded with immutable plan and Terraform SHA-256 attestations.'; await load();
  } catch (error) { message.textContent=error.message; }
});
document.querySelector('#apply-btn').addEventListener('click', async () => {
  const message = document.querySelector('#action-message');
  try {
    const result=await post(`/api/v1/recommendations/${currentRecommendationId}/simulate-apply`);
    message.textContent = result.status === 'SUCCEEDED'
      ? `Simulation succeeded: ${money(result.realized_monthly_savings_usd)} realized; no cloud command executed.`
      : result.status === 'ROLLED_BACK'
        ? `SLO breach: baseline restore verified; final realized savings ${money(result.realized_monthly_savings_usd)}.`
        : 'SLO breach: rollback could not be independently verified; workflow failed closed.';
    await load();
  } catch (error) { message.textContent=error.message; }
});

load().catch(error => { document.querySelector('#action-message').textContent=error.message; });
