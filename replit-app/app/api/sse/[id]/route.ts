import { NextRequest } from "next/server";
import { query } from "@/lib/db";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      const send = (event: string, data: unknown) => {
        controller.enqueue(
          encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
        );
      };

      // Send initial data
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

      // Poll every 30 seconds
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
          // Silently continue on poll errors
        }
      }, 30000);

      // Cleanup on close
      _request.signal.addEventListener("abort", () => {
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
