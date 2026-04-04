"use client";

interface MemoryEpisode {
  id: string;
  episode_type: string;
  summary: string;
  occurred_at: string;
}

interface AgentMemoryLogProps {
  episodes: MemoryEpisode[];
}

const typeColors: Record<string, string> = {
  crisis_detected: "bg-red-100 text-red-800",
  insight: "bg-blue-100 text-blue-800",
  improvement: "bg-emerald-100 text-emerald-800",
};

export default function AgentMemoryLog({ episodes }: AgentMemoryLogProps) {
  if (episodes.length === 0) {
    return (
      <div className="rounded-xl border p-4 text-center text-gray-400">
        No agent memory episodes
      </div>
    );
  }

  // Most recent first
  const sorted = [...episodes].sort(
    (a, b) =>
      new Date(b.occurred_at).getTime() - new Date(a.occurred_at).getTime()
  );

  return (
    <div className="rounded-xl border overflow-hidden">
      <div className="divide-y">
        {sorted.map((episode) => (
          <div
            key={episode.id}
            data-testid="memory-episode"
            className="p-4 hover:bg-gray-50"
          >
            <div className="flex items-start gap-3">
              <span
                className={`text-xs px-2 py-0.5 rounded-full font-medium whitespace-nowrap ${
                  typeColors[episode.episode_type] ||
                  "bg-gray-100 text-gray-800"
                }`}
              >
                {episode.episode_type.replace("_", " ")}
              </span>
              <div className="flex-1 min-w-0">
                <p className="text-sm text-gray-800">{episode.summary}</p>
                <p className="text-xs text-gray-400 mt-1">
                  {new Date(episode.occurred_at).toLocaleDateString()} at{" "}
                  {new Date(episode.occurred_at).toLocaleTimeString()}
                </p>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
