const MODEL = "@cf/meta/llama-3.1-8b-instruct";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type,Authorization",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return json({ ok: true, service: "cej-app-ai" });
    }

    if (url.pathname !== "/suggest" || request.method !== "POST") {
      return json({ ok: false, error: "Route inconnue." }, 404);
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ ok: false, error: "JSON invalide." }, 400);
    }

    const userMessage = String(payload.message || "").trim();
    if (!userMessage) {
      return json({ ok: false, error: "Message utilisateur requis." }, 400);
    }

    const appState = sanitizeState(payload.state);
    const aiResult = await env.AI.run(MODEL, {
      messages: [
        { role: "system", content: systemPrompt() },
        {
          role: "user",
          content: JSON.stringify(
            {
              message: userMessage,
              state: appState,
            },
            null,
            2,
          ),
        },
      ],
      temperature: 0.1,
      max_tokens: 900,
    });

    const raw = extractAiText(aiResult);
    const parsed = parseJsonObject(raw);
    if (!parsed) {
      return json(
        {
          ok: false,
          error: "Reponse IA non structuree.",
          raw,
        },
        502,
      );
    }

    const normalized = normalizeAiCommand(parsed);
    return json({
      ok: normalized.ok,
      command: normalized.command,
      explanation: normalized.explanation,
      needs_confirmation: normalized.needs_confirmation,
      error: normalized.error,
      raw,
    });
  },
};

function systemPrompt() {
  return [
    "Tu es l'assistant IA local d'un tableau de bord CEJ.",
    "Ton role est de transformer une demande utilisateur en commande JSON pour l'application.",
    "Tu peux aider a naviguer, charger/analyser une semaine, creer, modifier, deplacer ou supprimer une action.",
    "Tu ne dois pas inventer d'action existante: si une action doit etre modifiee, utilise un titre/id present dans state.actions.",
    "Si la demande est ambigue, renvoie une commande refusant l'execution avec une question courte.",
    "Retourne uniquement un objet JSON, sans Markdown.",
    "Schema attendu:",
    '{"ok":true,"needs_confirmation":true,"explanation":"...","command":{"type":"move_action","query":"...","date":"YYYY-MM-DD"}}',
    "Types autorises: show_agenda, show_settings, load_week, analyze_week, week_offset, current_week, move_action, update_action, create_action, delete_action.",
    "Pour update_action, utilise command.changes avec seulement: title, comment, due, qualification, status.",
    "Pour create_action, utilise title, comment, due, qualification.",
    "needs_confirmation doit etre true pour create_action, update_action, move_action et delete_action.",
    "needs_confirmation peut etre false pour show_agenda, show_settings, load_week, analyze_week, week_offset et current_week.",
    "Statuts autorises: not_started, in_progress, done, canceled.",
    "Categories autorisees: EMPLOI, PROJET_PROFESSIONNEL, CULTURE_SPORT_LOISIRS, CITOYENNETE, FORMATION, LOGEMENT, SANTE.",
  ].join("\n");
}

function sanitizeState(state) {
  const source = state && typeof state === "object" ? state : {};
  const actions = Array.isArray(source.actions) ? source.actions : [];
  return {
    weekStart: typeof source.weekStart === "string" ? source.weekStart : "",
    weekEnd: typeof source.weekEnd === "string" ? source.weekEnd : "",
    actions: actions.slice(0, 80).map((action) => ({
      id: stringField(action.id),
      title: stringField(action.title),
      comment: stringField(action.comment).slice(0, 500),
      date: stringField(action.date),
      status: stringField(action.status),
      qualification: stringField(action.qualification),
    })),
  };
}

function stringField(value) {
  return typeof value === "string" ? value : "";
}

function extractAiText(result) {
  if (!result || typeof result !== "object") return "";
  if (typeof result.response === "string") return result.response;
  if (typeof result.result === "string") return result.result;
  if (Array.isArray(result.response)) {
    return result.response.map((item) => item?.text || item?.content || "").join("\n");
  }
  return JSON.stringify(result);
}

function parseJsonObject(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    const match = text.match(/\{[\s\S]*\}/);
    if (!match) return null;
    try {
      return JSON.parse(match[0]);
    } catch {
      return null;
    }
  }
}

function normalizeAiCommand(value) {
  const command = value.command && typeof value.command === "object" ? value.command : {};
  const type = typeof command.type === "string" ? command.type : "";
  const allowedTypes = new Set([
    "show_agenda",
    "show_settings",
    "load_week",
    "analyze_week",
    "week_offset",
    "current_week",
    "move_action",
    "update_action",
    "create_action",
    "delete_action",
  ]);

  if (!value.ok || !allowedTypes.has(type)) {
    return {
      ok: false,
      error: typeof value.error === "string" ? value.error : "Commande IA refusee ou inconnue.",
      explanation: typeof value.explanation === "string" ? value.explanation : "",
      needs_confirmation: true,
      command: null,
    };
  }

  const safeCommand = coerceCommand(command);
  return {
    ok: true,
    command: safeCommand,
    explanation: typeof value.explanation === "string" ? value.explanation : "",
    needs_confirmation: Boolean(value.needs_confirmation),
  };
}

function coerceCommand(command) {
  const copy = { ...command };
  if (copy.type === "week_offset") {
    const offset = Number(copy.offset ?? copy.query ?? copy.value ?? 0);
    copy.offset = Number.isFinite(offset) && offset !== 0 ? Math.sign(offset) : 0;
    delete copy.query;
    delete copy.value;
    delete copy.date;
  }
  if (copy.type === "move_action" && !copy.date && copy.due) {
    copy.date = copy.due;
  }
  if (copy.type === "create_action" && !copy.due && copy.date) {
    copy.due = copy.date;
  }
  return copy;
}

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}
