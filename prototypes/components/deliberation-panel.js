/**
 * deliberation-panel.js
 * Deliberation Engine UI widget for the provider dashboard.
 * Renders the five output categories from the most recent deliberation.
 * Integrates with existing MCP server via claude-client.js (_mcpPost/_mcpGet).
 *
 * Usage: include after claude-client.js in any prototype HTML file.
 *
 * Exposes: window.DeliberationPanel
 */

window.DeliberationPanel = (function () {

  // ── API calls via existing claude-client pattern ───────────────────────────
  async function fetchDeliberationResults(patientId) {
    return await _mcpPost("/tools/get_deliberation_results", {
      patient_id: patientId,
      output_type: "all",
      limit: 1
    });
  }

  async function triggerDeliberation(patientId, triggerType) {
    return await _mcpPost("/tools/run_deliberation", {
      patient_id: patientId,
      trigger_type: triggerType || "manual",
      max_rounds: 3
    });
  }

  async function fetchPendingNudges(patientId) {
    return await _mcpPost("/tools/get_pending_nudges", {
      patient_id: patientId,
      target: "care_team"
    });
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  function renderScenario(s) {
    var probPct = Math.round(s.probability * 100);
    var confPct = Math.round(s.confidence * 100);
    var badge = s.timeframe === "next_30_days" ? "30d" :
                s.timeframe === "next_90_days" ? "90d" : "6mo";
    return '<div class="deliberation-scenario ' + (s.probability > 0.7 ? 'high-prob' : '') + '">' +
      '<div class="scenario-header">' +
        '<span class="scenario-badge">' + badge + '</span>' +
        '<span class="scenario-title">' + s.title + '</span>' +
        '<span class="scenario-prob">' + probPct + '%</span>' +
      '</div>' +
      '<p class="scenario-desc">' + s.description + '</p>' +
      (s.dissenting_view
        ? '<div class="dissent-flag">Model disagreement: ' + s.dissenting_view + '</div>'
        : '') +
      '<div class="scenario-footer">' +
        '<span class="conf-label">Confidence: ' + confPct + '%</span>' +
        '<span class="implication">' + s.clinical_implications + '</span>' +
      '</div>' +
    '</div>';
  }

  function renderFlag(f) {
    var iconMap = { critical: "red", high: "orange", medium: "gold", low: "steelblue" };
    var color = iconMap[f.priority] || "gray";
    return '<div class="data-flag ' + f.priority + '">' +
      '<span class="flag-icon" style="color:' + color + '">&#9679;</span>' +
      '<div class="flag-body">' +
        '<strong>' + f.data_type.replace(/_/g, ' ') + '</strong>' +
        '<p>' + f.description + '</p>' +
        '<em>' + f.recommended_action + '</em>' +
        (f.both_models_agreed ? '<span class="consensus-badge">Both models flagged</span>' : '') +
      '</div>' +
    '</div>';
  }

  function renderQuestion(q) {
    return '<div class="predicted-question">' +
      '<div class="q-text">Q: ' + q.question +
        ' <span class="q-likelihood">' + Math.round(q.likelihood * 100) + '%</span>' +
      '</div>' +
      '<div class="q-response">' +
        '<strong>Suggested response:</strong> ' + q.suggested_response +
      '</div>' +
    '</div>';
  }

  function renderNudge(n) {
    var sms = (n.output_data && n.output_data.channels && n.output_data.channels.sms) || "";
    return '<div class="nudge-card ' + n.output_type + '">' +
      '<div class="nudge-trigger">' + n.trigger_condition + '</div>' +
      '<div class="nudge-sms">' + sms + '</div>' +
      '<div class="nudge-technique">' + ((n.output_data && n.output_data.behavioral_technique) || '') + '</div>' +
    '</div>';
  }

  function renderPanel(data, containerId) {
    var container = document.getElementById(containerId);
    if (!container) return;

    if (!data || !data.deliberations || data.deliberations.length === 0) {
      container.innerHTML =
        '<div class="deliberation-empty">' +
          '<p>No deliberation results yet for this patient.</p>' +
          '<button class="btn-trigger-deliberation" ' +
                  'onclick="DeliberationPanel.trigger(\'' + (data && data.patient_id || '') + '\', \'' + containerId + '\')">' +
            'Run Deliberation Now' +
          '</button>' +
        '</div>';
      return;
    }

    var dlb = data.deliberations[0];
    var outputs = dlb.outputs || [];

    var scenarios = outputs.filter(function(o) { return o.output_type === 'anticipatory_scenario'; })
                           .map(function(o) { return o.output_data; });
    var questions = outputs.filter(function(o) { return o.output_type === 'predicted_patient_question'; })
                           .map(function(o) { return o.output_data; });
    var flags = outputs.filter(function(o) { return o.output_type === 'missing_data_flag'; })
                       .map(function(o) { return o.output_data; });
    var nudges = outputs.filter(function(o) { return o.output_type.indexOf('nudge') !== -1; });

    var dateStr = new Date(dlb.triggered_at).toLocaleDateString();
    var convPct = Math.round((dlb.convergence_score || 0) * 100);

    var html = '<div class="deliberation-panel">' +
      '<div class="deliberation-header">' +
        '<span class="dlb-meta">Last deliberation: ' + dateStr + '</span>' +
        '<span class="dlb-convergence" title="How much Claude and GPT-4 agreed">' +
          'Model convergence: ' + convPct + '%' +
        '</span>' +
        '<span class="dlb-rounds">' + dlb.rounds_completed + ' debate round(s)</span>' +
        '<button class="btn-trigger-small" ' +
                'onclick="DeliberationPanel.trigger(\'' + (data.patient_id || '') + '\', \'' + containerId + '\')">' +
          'Re-run' +
        '</button>' +
      '</div>';

    // Anticipatory Scenarios
    if (scenarios.length) {
      html += '<section class="dlb-section"><h4>Anticipatory Scenarios</h4>' +
        scenarios.map(renderScenario).join('') + '</section>';
    }

    // Missing Data Flags
    if (flags.length) {
      html += '<section class="dlb-section"><h4>Missing Data Flags</h4>' +
        flags.map(renderFlag).join('') + '</section>';
    }

    // Predicted Patient Questions
    if (questions.length) {
      html += '<section class="dlb-section"><h4>Patient May Ask</h4>' +
        questions.map(renderQuestion).join('') + '</section>';
    }

    // Nudge Queue Preview
    if (nudges.length) {
      html += '<section class="dlb-section"><h4>Queued Nudges</h4>' +
        nudges.map(renderNudge).join('') + '</section>';
    }

    html += '</div>';
    container.innerHTML = html;
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  async function load(patientId, containerId) {
    var container = document.getElementById(containerId);
    if (container) {
      container.innerHTML = '<div class="deliberation-loading">' +
        'Running clinical deliberation analysis...</div>';
    }
    try {
      var data = await fetchDeliberationResults(patientId);
      renderPanel(Object.assign({}, data, { patient_id: patientId }), containerId);
    } catch (e) {
      if (container) {
        container.innerHTML = '<div class="deliberation-error">' +
          'Failed to load deliberation results: ' + e.message + '</div>';
      }
    }
  }

  async function trigger(patientId, containerId) {
    var container = document.getElementById(containerId);
    if (container) {
      container.innerHTML = '<div class="deliberation-loading">' +
        'Running dual-LLM deliberation... Claude + GPT-4 debating...<br>' +
        '<small>This takes ~60 seconds</small></div>';
    }
    try {
      await triggerDeliberation(patientId);
      await load(patientId, containerId);
    } catch (e) {
      if (container) {
        container.innerHTML = '<div class="deliberation-error">' +
          'Deliberation failed: ' + e.message + '</div>';
      }
    }
  }

  return { load: load, trigger: trigger };
})();
