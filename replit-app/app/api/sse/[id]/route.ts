import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
import { requireBearerToken } from "@/lib/auth-middleware";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const authError = await requireBearerToken(request);
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
