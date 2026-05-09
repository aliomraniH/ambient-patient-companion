/**
 * POST /api/modal/webhook
 *
 * Public entry point for Modal training callbacks.
 * 1. Reads the raw request body.
 * 2. Verifies the X-Hub-Signature-256 HMAC-SHA256 signature using
 *    MODAL_WEBHOOK_SECRET (matches the scheme used by trigger_lora_training).
 * 3. Forwards the verified payload to the Skills MCP server's internal
 *    REST endpoint /tools/modal_webhook_internal to update lora_training_runs.
 */

import { NextRequest, NextResponse } from "next/server";
import { createHmac, timingSafeEqual } from "crypto";

const SKILLS_BASE = "http://localhost:8002";

function verifySignature(body: Buffer, signatureHeader: string, secret: string): boolean {
  try {
    const expected = "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
    const a = Buffer.from(expected, "utf8");
    const b = Buffer.from(signatureHeader, "utf8");
    if (a.length !== b.length) return false;
    return timingSafeEqual(a, b);
  } catch {
    return false;
  }
}

export async function POST(request: NextRequest) {
  const secret = process.env.MODAL_WEBHOOK_SECRET;
  if (!secret) {
    console.error("[modal/webhook] MODAL_WEBHOOK_SECRET is not set");
    return NextResponse.json({ error: "Webhook secret not configured" }, { status: 500 });
  }

  const signatureHeader = request.headers.get("x-hub-signature-256") ?? "";
  if (!signatureHeader) {
    return NextResponse.json({ error: "Missing X-Hub-Signature-256 header" }, { status: 401 });
  }

  const rawBody = Buffer.from(await request.arrayBuffer());

  if (!verifySignature(rawBody, signatureHeader, secret)) {
    console.warn("[modal/webhook] HMAC verification failed");
    return NextResponse.json({ error: "Invalid signature" }, { status: 401 });
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(rawBody.toString("utf8"));
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  try {
    const skillsResp = await fetch(`${SKILLS_BASE}/tools/modal_webhook_internal`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await skillsResp.json();
    return NextResponse.json(result, { status: skillsResp.ok ? 200 : 400 });
  } catch (err) {
    console.error("[modal/webhook] Skills server call failed:", err);
    return NextResponse.json({ error: "Skills server unreachable" }, { status: 502 });
  }
}
