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
 * Override by setting window.FASTMCP_BASE_URL before loading this script.
 * @type {string}
 */
const FASTMCP_BASE_URL = (typeof window !== 'undefined' && window.FASTMCP_BASE_URL)
  || 'http://localhost:8000';

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

// Log readiness on load
console.log('Ambient clinical layer ready — Phase 1');
