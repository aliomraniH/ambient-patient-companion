/**
 * @fileoverview Shared client for the Clinical Intelligence FastMCP server.
 *
 * Wraps calls to the FastMCP server, handles loading states, and provides
 * consistent error handling. All AI calls from HTML prototypes route through
 * this client — prototypes never call Claude API directly.
 *
 * @module claude-client
 */

/* global fetch */

/**
 * Base URL for the FastMCP clinical intelligence server.
 *
 * Resolution order:
 *  1. window.FASTMCP_BASE_URL — set before loading this script to override
 *  2. Browser context → relative proxy path (/api/mcp/8001) so the request
 *     is forwarded by Next.js without exposing localhost to the browser
 *  3. Node / server context → direct localhost URL (used in tests)
 *
 * @type {string}
 */
const FASTMCP_BASE_URL = (typeof window !== 'undefined' && window.FASTMCP_BASE_URL)
  || (typeof window !== 'undefined' ? '/api/mcp/8001' : 'http://localhost:8001');

/**
 * @typedef {Object} ClinicalResponse
 * @property {string} status - One of: 'success', 'warning', 'blocked', 'escalated', 'error'
 * @property {string|null} recommendation - The clinical recommendation text
 * @property {string} [reason] - Reason for blocked/escalated/error status
 * @property {Array<Object>} citations - List of guideline citations used
 * @property {Array<Object>} escalation_flags - Any escalation triggers detected
 * @property {Array<string>} [validation_flags] - Output validation warnings
 */

/**
 * @typedef {Object} ScreeningResult
 * @property {string} screening_name - Name of the screening
 * @property {string} recommendation_id - Guideline recommendation ID
 * @property {string} uspstf_grade - USPSTF evidence grade (A, B, C, D, I)
 * @property {string} recommendation_text - Full recommendation text
 * @property {string} guideline_source - Source organization (e.g., 'USPSTF')
 * @property {string} version - Guideline version year
 */

/**
 * @typedef {Object} DrugInteraction
 * @property {string} drug_a - First drug in the interaction pair
 * @property {string} drug_b - Second drug in the interaction pair
 * @property {string} severity - Interaction severity: 'high', 'moderate', 'low'
 * @property {string} description - Description of the interaction
 * @property {string} action - Recommended action
 */

/**
 * Make a POST request to the FastMCP server.
 *
 * @param {string} endpoint - The API endpoint path
 * @param {Object} body - The request body
 * @returns {Promise<Object>} The parsed JSON response
 */
async function _mcpPost(endpoint, body) {
  try {
    const response = await fetch(`${FASTMCP_BASE_URL}${endpoint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!response.ok) {
      return {
        status: 'error',
        reason: `Server returned ${response.status}: ${response.statusText}`,
        recommendation: null,
        citations: [],
        escalation_flags: [],
      };
    }

    return await response.json();
  } catch (err) {
    return {
      status: 'error',
      reason: `Network error: ${err.message}`,
      recommendation: null,
      citations: [],
      escalation_flags: [],
    };
  }
}

/**
 * Make a GET request to the FastMCP server.
 *
 * @param {string} endpoint - The API endpoint path with query params
 * @returns {Promise<Object>} The parsed JSON response
 */
async function _mcpGet(endpoint) {
  try {
    const response = await fetch(`${FASTMCP_BASE_URL}${endpoint}`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });

    if (!response.ok) {
      return {
        status: 'error',
        reason: `Server returned ${response.status}: ${response.statusText}`,
      };
    }

    return await response.json();
  } catch (err) {
    return {
      status: 'error',
      reason: `Network error: ${err.message}`,
    };
  }
}

/**
 * Send a clinical query through the guardrail pipeline.
 *
 * Routes the query through the FastMCP server's three-layer safety pipeline:
 * input validation → Claude API generation → output validation.
 *
 * @param {string} query - The clinical question or request
 * @param {string} role - One of: 'pcp', 'care_manager', 'patient'
 * @param {Object} patientContext - Patient information (conditions, medications, labs, etc.)
 * @returns {Promise<ClinicalResponse>} The clinical response with status and recommendation
 */
async function queryClinical(query, role, patientContext) {
  return _mcpPost('/tools/clinical_query', {
    query: query,
    role: role,
    patient_context: patientContext || {},
  });
}

/**
 * Check which USPSTF screenings are due for a patient.
 *
 * @param {number} patientAge - Patient's age in years
 * @param {string} sex - Patient's sex ('male' or 'female')
 * @param {Array<string>} conditions - List of patient conditions
 * @returns {Promise<Array<ScreeningResult>>} Array of applicable screenings
 */
async function checkScreeningsDue(patientAge, sex, conditions) {
  const result = await _mcpPost('/tools/check_screening_due', {
    patient_age: patientAge,
    sex: sex,
    conditions: conditions || [],
  });

  // Normalize: the tool returns an array directly, but wrap errors consistently
  if (result.status === 'error') {
    return [];
  }
  return Array.isArray(result) ? result : [];
}

/**
 * Check for drug interactions among a list of medications.
 *
 * @param {Array<string>} medications - List of medication names
 * @returns {Promise<Array<DrugInteraction>>} Array of detected interactions
 */
async function checkDrugInteractions(medications) {
  const result = await _mcpPost('/tools/flag_drug_interaction', {
    medications: medications || [],
  });

  if (result.status === 'error') {
    return [];
  }
  return Array.isArray(result) ? result : [];
}

/**
 * Fetch a specific guideline by recommendation ID.
 *
 * @param {string} recommendationId - The guideline recommendation ID
 * @returns {Promise<Object>} The guideline entry or error object
 */
async function getGuideline(recommendationId) {
  return _mcpGet(`/tools/get_guideline?recommendation_id=${encodeURIComponent(recommendationId)}`);
}

/**
 * Fetch synthetic patient data by MRN.
 *
 * @param {string} mrn - Medical record number (e.g., '4829341' for Maria Chen)
 * @returns {Promise<Object>} The patient data or error object
 */
async function getSyntheticPatient(mrn) {
  return _mcpGet(`/tools/get_synthetic_patient?mrn=${encodeURIComponent(mrn)}`);
}

/**
 * Register a HealthEx patient and obtain their internal UUID.
 *
 * @param {Object|string} healthSummaryJson - Patient health summary (object or JSON string)
 * @returns {Promise<Object>} Object containing patient_id (UUID), mrn, name, and status
 */
async function registerHealthexPatient(healthSummaryJson) {
  const payload = typeof healthSummaryJson === 'string'
    ? healthSummaryJson
    : JSON.stringify(healthSummaryJson);
  return _mcpPost('/tools/register_healthex_patient', {
    health_summary_json: payload,
  });
}

/**
 * Ingest HealthEx FHIR data into the patient warehouse.
 *
 * Call this once per resource_type: labs | medications | conditions | encounters | summary
 *
 * @param {string} patientId - UUID from registerHealthexPatient
 * @param {string} resourceType - One of: labs | medications | conditions | encounters | summary
 * @param {Object|string} fhirJson - Raw JSON from the HealthEx tool response (object or JSON string)
 * @returns {Promise<Object>} Ingest result with records_written, resource_type, duration_ms
 */
async function ingestFromHealthex(patientId, resourceType, fhirJson) {
  const payload = typeof fhirJson === 'string' ? fhirJson : JSON.stringify(fhirJson);
  return _mcpPost('/tools/ingest_from_healthex', {
    patient_id: patientId,
    resource_type: resourceType,
    fhir_json: payload,
  });
}

/**
 * Trigger a full Dual-LLM deliberation session for a patient.
 *
 * @param {string} patientId - Patient UUID or MRN
 * @param {string} [triggerType='manual'] - One of: manual | scheduled_pre_encounter |
 *   lab_result_received | medication_change | missed_appointment | temporal_threshold
 * @param {number} [maxRounds=3] - Maximum cross-critique rounds (1–5)
 * @returns {Promise<Object>} Deliberation summary with convergence_score and output counts
 */
async function runDeliberation(patientId, triggerType = 'manual', maxRounds = 3) {
  return _mcpPost('/tools/run_deliberation', {
    patient_id: patientId,
    trigger_type: triggerType,
    max_rounds: maxRounds,
  });
}

/**
 * Retrieve outputs from the most recent deliberation(s) for a patient.
 *
 * @param {string} patientId - Patient UUID or MRN
 * @param {string} [outputType='all'] - Filter by output type: all | anticipatory_scenario |
 *   predicted_patient_question | missing_data_flag | patient_nudge | care_team_nudge
 * @param {number} [limit=1] - Number of most recent deliberations to return
 * @returns {Promise<Object>} Structured deliberation outputs
 */
async function getDeliberationResults(patientId, outputType = 'all', limit = 1) {
  return _mcpPost('/tools/get_deliberation_results', {
    patient_id: patientId,
    output_type: outputType,
    limit,
  });
}

/**
 * Retrieve pending (unsent) nudges for a patient.
 *
 * @param {string} patientId - Patient UUID or MRN
 * @param {string} [target='patient'] - 'patient' or 'care_team'
 * @returns {Promise<Object>} Pending nudges with channel content
 */
async function getPendingNudges(patientId, target = 'patient') {
  return _mcpPost('/tools/get_pending_nudges', {
    patient_id: patientId,
    target,
  });
}

// Log readiness on load
console.log('Ambient clinical layer ready — Phase 1 + HealthEx pipeline + Deliberation Engine');
