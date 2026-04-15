import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
import { oauthStore } from "@/lib/oauth-store";

function checkAuth(request: NextRequest): NextResponse | null {
  const authHeader = request.headers.get("authorization");
  if (authHeader && authHeader.startsWith("Bearer ")) {
    const token = authHeader.slice(7);
    if (oauthStore.validateToken(token)) return null;
  }

  const queryToken = request.nextUrl.searchParams.get("token");
  if (queryToken && oauthStore.validateToken(queryToken)) return null;

  return NextResponse.json(
    { error: "unauthorized", error_description: "Bearer token required" },
    { status: 401 }
  );
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const authError = checkAuth(request);
  if (authError) return authError;

  const { id } = await params;

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: unknown) => {
        controller.enqueue(
          encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
        );
      };

      try {
        const rows = await query(
          `SELECT score, primary_driver, trend_direction, confidence, score_date
           FROM obt_scores WHERE patient_id = $1
           ORDER BY score_date DESC LIMIT 1`,
          [id]
        );
        if (rows.length > 0) {
          send("obt-update", rows[0]);
        }
      } catch (error) {
        send("error", { message: "Failed to fetch initial data" });
      }

      const interval = setInterval(async () => {
        try {
          const rows = await query(
            `SELECT score, primary_driver, trend_direction, confidence, score_date
             FROM obt_scores WHERE patient_id = $1
             ORDER BY score_date DESC LIMIT 1`,
            [id]
          );
          if (rows.length > 0) {
            send("obt-update", rows[0]);
          }
        } catch {
        }
      }, 30000);

      request.signal.addEventListener("abort", () => {
        clearInterval(interval);
        controller.close();
      });
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
