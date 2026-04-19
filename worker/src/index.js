const DEFAULT_MODEL = "@cf/zai-org/glm-4.7-flash";
const DAILY_REQUEST_LIMIT = 120;
const DAILY_NEURON_LIMIT = 10000;
const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders });

    const url = new URL(request.url);
    if (url.pathname === "/health") return json({ ok: true, service: "cej-app-ai" });
    if (url.pathname === "/usage") return json({ ok: true, usage: usageSnapshot(env) });
    if (!["/chat", "/suggest"].includes(url.pathname) || request.method !== "POST") {
      return json({ ok: false, error: "Route inconnue." }, 404);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ ok: false, error: "JSON invalide." }, 400);
    }

    const usage = usageSnapshot(env);
    if (usage.blocked) {
      return json({
        ok: false,
        message: "Yoki est bloque par le garde-fou quota IA local.",
        error: "quota_guard_reached",
        usage,
      }, 429);
    }

    const state = sanitizeState(payload.state);
    const history = sanitizeHistory(payload.history);
    const message = String(payload.message || "").trim();
    if (!message) return json({ ok: false, error: "Message utilisateur requis." }, 400);

    const model = env.AI_MODEL || DEFAULT_MODEL;
    const aiResult = await env.AI.run(model, {
      messages: [
        { role: "system", content: systemPrompt() },
        {
          role: "user",
          content: JSON.stringify({
            message,
            history,
            state,
            settings: sanitizeSettings(payload.settings),
            available_tools: availableTools(),
            response_schema: {
              message: "string",
              tool_call: { name: "string", arguments: "object" },
              confidence: "number 0..1",
              needs_confirmation: "boolean"
            }
          }, null, 2),
        },
      ],
      temperature: 0,
      max_tokens: 700,
      max_completion_tokens: 700,
    });

    const raw = extractAiText(aiResult);
    const parsed = parseJsonObject(raw) || fallbackResponse(message, state);

    const normalized = normalizeChatResponse(parsed, state);
    const response = {
      ok: normalized.ok,
      message: normalized.message,
      tool_call: normalized.tool_call,
      confidence: normalized.confidence,
      needs_confirmation: normalized.needs_confirmation,
      error: normalized.error,
      model,
      usage: { ...usage, requests_today: usage.requests_today + 1 },
    };

    if (url.pathname === "/suggest") {
      response.command = normalized.tool_call ? toolCallToLegacyCommand(normalized.tool_call) : null;
      response.explanation = normalized.message;
    }
    return json(response);
  },
};

function systemPrompt() {
  return [
    "Tu es Yoki, assistant conversationnel pour une app CEJ.",
    "IMPORTANT: ta sortie doit etre uniquement un objet JSON valide. Aucun Markdown, aucune analyse, aucune etape, aucun texte hors JSON.",
    "N'ecris jamais ton raisonnement. Ne commence jamais par une liste numerotee.",
    "Tu parles naturellement en francais, court, utile, sans exposer de JSON.",
    "Tu dois décider si tu réponds seulement, si tu demandes une précision, ou si tu appelles un outil.",
    "Ne crée pas une action si les informations essentielles manquent: titre/activité et date.",
    "Si l'utilisateur demande si une activité passe en CEJ, conseille une catégorie puis demande s'il veut préparer l'action.",
    "Quand l'utilisateur confirme une intention précédente, utilise l'historique.",
    "Pour les outils, retourne toujours un JSON strict: message, tool_call, confidence, needs_confirmation.",
    'Format exact: {"message":"...","tool_call":null,"confidence":0.8,"needs_confirmation":false}',
    "tool_call peut être null si tu poses une question ou réponds sans action.",
    "Utilise create_action seulement quand title, due et qualification sont connus.",
    "Utilise update_action/move_action seulement si la cible est claire dans state.actions ou via query précis.",
    "Suppression: propose delete_action mais needs_confirmation doit être true.",
    "Catégories autorisées: EMPLOI, PROJET_PROFESSIONNEL, CULTURE_SPORT_LOISIRS, CITOYENNETE, FORMATION, LOGEMENT, SANTE.",
    "Statuts autorisés: not_started, in_progress, done, canceled.",
    "Dates: toujours YYYY-MM-DD. Résous les dates relatives depuis state.today ou state.weekStart si possible.",
    "N'invente pas de preuve ni d'information non donnée. Si le lieu est inconnu, dis simplement lieu non précisé.",
  ].join("\n");
}

function availableTools() {
  return [
    { name: "create_action", args: ["title", "comment", "due", "qualification", "status"] },
    { name: "update_action", args: ["query", "id", "changes"] },
    { name: "move_action", args: ["query", "id", "due"] },
    { name: "delete_action", args: ["query", "id"] },
    { name: "change_week", args: ["offset", "week_start"] },
    { name: "analyze_week", args: [] },
    { name: "ask_user", args: ["question"] },
    { name: "propose_action", args: ["title", "comment", "due", "qualification"] },
  ];
}

function sanitizeState(state) {
  const source = state && typeof state === "object" ? state : {};
  const actions = Array.isArray(source.actions) ? source.actions : [];
  return {
    today: stringField(source.today),
    weekStart: stringField(source.weekStart),
    weekEnd: stringField(source.weekEnd),
    actions: actions.slice(0, 80).map((action) => ({
      id: stringField(action.id),
      title: stringField(action.title),
      comment: stringField(action.comment).slice(0, 400),
      date: stringField(action.date),
      status: stringField(action.status),
      qualification: stringField(action.qualification),
    })),
  };
}

function sanitizeHistory(history) {
  if (!Array.isArray(history)) return [];
  return history.slice(-10).map((item) => ({
    role: item?.role === "assistant" ? "assistant" : "user",
    content: stringField(item?.content).slice(0, 1000),
  })).filter((item) => item.content);
}

function sanitizeSettings(settings) {
  const mode = stringField(settings?.autonomyMode);
  return {
    autonomyMode: ["strict", "prudent", "normal"].includes(mode) ? mode : "normal",
    confidenceThreshold: Number(settings?.confidenceThreshold || 0.75),
  };
}

function normalizeChatResponse(value, state) {
  const message = stringField(value.message || value.explanation || value.error || "Je ne suis pas sur de l'action a effectuer.");
  const confidence = clamp(Number(value.confidence ?? 0.65), 0, 1);
  const toolCall = normalizeToolCall(value.tool_call || value.toolCall || value.command, state);
  return {
    ok: true,
    message,
    tool_call: toolCall,
    confidence,
    needs_confirmation: toolCall ? needsConfirmation(toolCall.name, value.needs_confirmation) : false,
  };
}

function normalizeToolCall(raw, state) {
  if (!raw || typeof raw !== "object") return null;
  const name = stringField(raw.name || raw.type || raw.action);
  const allowed = new Set(availableTools().map((tool) => tool.name));
  if (!allowed.has(name)) return null;
  const args = raw.arguments && typeof raw.arguments === "object" ? { ...raw.arguments } : { ...raw };
  delete args.name;
  delete args.type;
  delete args.action;

  if (name === "create_action" || name === "propose_action") {
    args.due = normalizeDue(args.due || args.date, state);
    args.qualification = allowedQualification(args.qualification) ? args.qualification : inferQualification(`${args.title || ""} ${args.comment || ""}`);
    args.status = allowedStatus(args.status) ? args.status : "done";
  }
  if (name === "move_action") args.due = normalizeDue(args.due || args.date, state);
  if (name === "change_week") args.offset = Number(args.offset || 0);
  return { name, arguments: args };
}

function toolCallToLegacyCommand(toolCall) {
  if (!toolCall) return null;
  const args = toolCall.arguments || {};
  if (toolCall.name === "change_week") return { type: "week_offset", offset: args.offset || 0 };
  if (toolCall.name === "analyze_week") return { type: "analyze_week" };
  return { type: toolCall.name, ...args };
}

function needsConfirmation(name, requested) {
  if (name === "delete_action") return true;
  if (name === "propose_action") return true;
  return Boolean(requested);
}

function normalizeDue(value, state) {
  const raw = stringField(value);
  if (/^20\d{2}-\d{2}-\d{2}$/.test(raw)) return raw;
  const normalized = normalizeText(raw);
  const relative = { lundi: 0, mardi: 1, mercredi: 2, jeudi: 3, vendredi: 4, samedi: 5, dimanche: 6 };
  for (const [day, offset] of Object.entries(relative)) {
    if (normalized.includes(day)) return addDaysIso(state.weekStart, offset);
  }
  if (normalized.includes("demain")) return addDaysIso(state.today, 1);
  if (normalized.includes("hier")) return addDaysIso(state.today, -1);
  return raw;
}

function allowedQualification(value) {
  return new Set(["EMPLOI", "PROJET_PROFESSIONNEL", "CULTURE_SPORT_LOISIRS", "CITOYENNETE", "FORMATION", "LOGEMENT", "SANTE"]).has(value);
}

function allowedStatus(value) {
  return new Set(["not_started", "in_progress", "done", "canceled"]).has(value);
}

function inferQualification(text) {
  const normalized = normalizeText(text);
  if (/\b(cinema|film|musee|theatre|sport|lecture|culture)\b/.test(normalized)) return "CULTURE_SPORT_LOISIRS";
  if (/\b(formation|cours|code|auto ecole|permis)\b/.test(normalized)) return "FORMATION";
  if (/\b(logement|appartement|loyer)\b/.test(normalized)) return "LOGEMENT";
  if (/\b(sante|medecin|psychologue)\b/.test(normalized)) return "SANTE";
  if (/\b(citoyen|administratif|caf|aide|demarche)\b/.test(normalized)) return "CITOYENNETE";
  return "EMPLOI";
}

function usageSnapshot(env) {
  const limit = Number(env.DAILY_REQUEST_LIMIT || DAILY_REQUEST_LIMIT);
  return {
    source: "worker-local-estimate",
    requests_today: 0,
    daily_limit: limit,
    neurons_today: 0,
    neuron_daily_limit: Number(env.DAILY_NEURON_LIMIT || DAILY_NEURON_LIMIT),
    blocked: false,
    note: "Estimation locale basee sur le quota gratuit indique: 10k neurones/jour. Ne remplace pas la facturation officielle Cloudflare.",
  };
}

function extractAiText(result) {
  if (!result || typeof result !== "object") return "";
  if (typeof result.response === "string") return result.response;
  if (typeof result.result === "string") return result.result;
  if (typeof result.choices?.[0]?.message?.content === "string") return result.choices[0].message.content;
  if (typeof result.choices?.[0]?.message?.reasoning === "string") return result.choices[0].message.reasoning;
  return JSON.stringify(result);
}

function parseJsonObject(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
    const candidate = fenced ? fenced[1] : text.match(/\{[\s\S]*\}/)?.[0];
    if (!candidate) return null;
    try {
      return JSON.parse(candidate);
    } catch {
      return null;
    }
  }
}

function fallbackResponse(message, state) {
  const normalized = normalizeText(message);
  const autoEcole = /\b(auto ecole|auto-ecole|permis|code)\b/.test(normalized);
  const asksEligibility = /\b(passe|cadre|cej|compte|valable|possible)\b/.test(normalized);
  const date = inferDateFromText(normalized, state);

  if (autoEcole && asksEligibility) {
    return {
      message: "Oui, une demarche a l'auto-ecole peut passer dans ton CEJ, plutot en FORMATION. Tu veux que je prepare l'action ? Si oui, dis-moi la date.",
      tool_call: null,
      confidence: 0.82,
      needs_confirmation: false,
    };
  }

  if (autoEcole && date) {
    return {
      message: "Je peux preparer une action FORMATION pour ta demarche a l'auto-ecole.",
      tool_call: {
        name: "propose_action",
        arguments: {
          title: "Demarches a l'auto-ecole",
          comment: "Demarches administratives realisees a l'auto-ecole dans le cadre du parcours permis.",
          due: date,
          qualification: "FORMATION",
        },
      },
      confidence: 0.78,
      needs_confirmation: true,
    };
  }

  return {
    message: "Je n'arrive pas a formuler une reponse fiable. Reformule avec l'action et la date, par exemple: auto-ecole le 2026-04-19.",
    tool_call: null,
    confidence: 0.2,
    needs_confirmation: false,
  };
}

function inferDateFromText(normalized, state) {
  const explicit = normalized.match(/\b(20\d{2}-\d{2}-\d{2})\b/);
  if (explicit) return explicit[1];
  if (normalized.includes("aujourd hui") || normalized.includes("aujourdhui")) return state.today;
  if (normalized.includes("demain")) return addDaysIso(state.today, 1);
  if (normalized.includes("hier")) return addDaysIso(state.today, -1);
  return "";
}

function normalizeText(value) {
  return String(value || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim();
}

function stringField(value) {
  return typeof value === "string" ? value : "";
}

function clamp(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, value));
}

function addDaysIso(isoDate, offset) {
  if (!/^20\d{2}-\d{2}-\d{2}$/.test(isoDate || "")) return "";
  const date = new Date(`${isoDate}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json; charset=utf-8" },
  });
}
