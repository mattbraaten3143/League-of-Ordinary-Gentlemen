// "Claude's take" for the trade calculator.
// The browser sends only the calculator's facts for the two sides; the prompt and the
// API key live here, so the endpoint can't be used as a general-purpose chatbot.
//   GET  /api/take  -> {ok: true} when the key is configured (the page shows the button only then)
//   POST /api/take  {sides: [...]} -> streamed plain text

const MODEL = process.env.TAKE_MODEL || "claude-haiku-5-5";
const MAX_BODY = 30000;            // bytes of facts accepted
const PER_IP = [8, 10 * 60e3];     // 8 takes per 10 minutes per visitor
const PER_DAY = [150, 24 * 3600e3]; // 150 takes per day per server instance (the hard cap is the Console spend limit)

const PROMPT = `You are the resident trade analyst for "The League of Ordinary Gentleman," a 12-team dynasty fantasy football league (1QB, full PPR, lineup QB, 2 RB, 3 WR, TE, 2 FLEX). The rookie draft order rewards a low Max Points For for non-playoff teams, so selling production can improve a team's own first-round pick, but only if the team owns that pick. A manager just entered this trade in the league site's calculator. The site's code has already done all the math and reached a verdict for each side; the facts are below as JSON.

Write the take the way a sharp friend in the league would talk it through over a beer: natural, specific to these two teams, a little personality, never robotic. Do not repeat the same sentence shapes for both teams.
Rules:
- Use only the facts given. Do not use anything you know about these players or teams from outside this message: no injuries, ages, reputations, news, stats or opinions that are not in the facts. If the facts do not say it, do not say it.
- No numbers at all: no percentages, point totals, values, ranks or pick slots. Describe size in words (a little, a lot, a long shot, all but locked in).
- Agree with each side's verdict; explain it, do not overturn it.
- For each team: say what the deal does for them given where they stand, then how it would look if they took the other path (pushing for the playoffs versus selling for the future). If counterIdea is present, suggest it naturally as what would make the deal work for them.
- Plain, direct sentences. No em dashes. No "not X, but Y" constructions.
Format exactly:
<first team name>
<one paragraph, 3 to 5 sentences>

<second team name>
<one paragraph, 3 to 5 sentences>

Bottom line: <one sentence>

FACTS:
`;

const hits = new Map(); // key -> timestamps (per server instance; best effort)
function allow(key, [n, ms]) {
  const now = Date.now(), list = (hits.get(key) || []).filter(t => now - t < ms);
  if (list.length >= n) { hits.set(key, list); return false; }
  list.push(now); hits.set(key, list); return true;
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
  status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

// Tolerate whitespace or quotes picked up when the key was pasted into Vercel
const apiKey = () => (process.env.ANTHROPIC_API_KEY || "").trim().replace(/^["']|["']$/g, "");

export function GET() {
  const k = apiKey();
  // shape only, never the key: lets the owner spot a mis-pasted value
  return json({ ok: !!k, keyShape: k ? { prefix: k.slice(0, 7), length: k.length } : null });
}

export async function POST(request) {
  const key = apiKey();
  if (!key) return json({ error: "not configured" }, 503);

  // Only this site's own pages may call it
  const origin = request.headers.get("origin") || "";
  let sameSite = false;
  try { sameSite = new URL(origin).host === request.headers.get("host"); } catch (e) { /* no origin */ }
  if (!sameSite) return json({ error: "forbidden" }, 403);

  const ip = (request.headers.get("x-forwarded-for") || "").split(",")[0].trim() || "unknown";
  if (!allow("ip:" + ip, PER_IP)) return json({ error: "Too many takes in a short time. Try again in a few minutes." }, 429);
  if (!allow("day", PER_DAY)) return json({ error: "Daily limit reached." }, 429);

  const raw = await request.text();
  if (raw.length > MAX_BODY) return json({ error: "too large" }, 413);
  let sides;
  try { sides = JSON.parse(raw).sides; } catch (e) { return json({ error: "bad request" }, 400); }
  if (!Array.isArray(sides) || sides.length !== 2) return json({ error: "bad request" }, 400);

  const upstream = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({
      model: MODEL, max_tokens: 700, stream: true,
      messages: [{ role: "user", content: PROMPT + JSON.stringify(sides, null, 1) }],
    }),
  });
  if (!upstream.ok || !upstream.body) {
    const detail = await upstream.text().catch(() => "");
    console.error("anthropic error", upstream.status, detail.slice(0, 500));
    return json({ error: "upstream " + upstream.status }, 502);
  }

  // Anthropic's event stream -> plain text
  const dec = new TextDecoder(), enc = new TextEncoder();
  let buf = "";
  const out = new ReadableStream({
    async start(controller) {
      const reader = upstream.body.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf("\n")) >= 0) {
            const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
            if (!line.startsWith("data:")) continue;
            let ev; try { ev = JSON.parse(line.slice(5)); } catch (e) { continue; }
            if (ev.type === "content_block_delta" && ev.delta && ev.delta.type === "text_delta") controller.enqueue(enc.encode(ev.delta.text));
          }
        }
      } catch (e) {
        console.error("stream error", e);
      } finally {
        controller.close();
      }
    },
  });
  return new Response(out, { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
}
