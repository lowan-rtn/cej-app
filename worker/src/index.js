const DEFAULT_MODEL = "@cf/meta/llama-3.1-8b-instruct";

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
    const deterministic = deterministicSuggest(userMessage, appState);
    if (deterministic) {
      return json({
        ...deterministic,
        model: "deterministic",
        raw: "",
      });
    }

    const model = env.AI_MODEL || DEFAULT_MODEL;
    const aiResult = await env.AI.run(model, {
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
      max_tokens: 1600,
      max_completion_tokens: 1600,
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

    const normalized = normalizeAiCommand(parsed, appState);
    return json({
      ok: normalized.ok,
      command: normalized.command,
      explanation: normalized.explanation,
      needs_confirmation: normalized.needs_confirmation,
      error: normalized.error,
      model,
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
    "Si une creation d'action a un titre et une date, renvoie create_action meme si la phrase contient des fautes.",
    "Si une creation d'action manque seulement le titre ou seulement la date, renvoie ok:false avec une question courte dans error.",
    "Si l'utilisateur demande si une activite peut passer en CEJ, reponds ok:false avec un conseil court et une question de confirmation, sans creer directement.",
    "Si l'utilisateur confirme mais qu'il manque le detail principal de l'activite, demande le detail avant de creer.",
    "Pour les activites culturelles comme cinema, utilise CULTURE_SPORT_LOISIRS et genere un titre/description propres quand les details sont disponibles.",
    "Les dates relatives doivent etre resolues avec state.weekStart/state.weekEnd: lundi=weekStart, dimanche=weekStart+6.",
    "N'utilise jamais une date relative brute comme 'dimanche' dans command.due: utilise toujours YYYY-MM-DD.",
    "Si la demande est ambigue, renvoie une commande refusant l'execution avec une question courte.",
    "Retourne uniquement un objet JSON, sans Markdown.",
    "Schema attendu:",
    '{"ok":true,"needs_confirmation":true,"explanation":"...","command":{"type":"move_action","query":"...","date":"YYYY-MM-DD"}}',
    "Types autorises: show_agenda, show_settings, load_week, analyze_week, week_offset, current_week, move_action, update_action, create_action, delete_action.",
    "Pour update_action, utilise command.changes avec seulement: title, comment, due, qualification, status.",
    "Pour create_action, utilise title, comment, due, qualification. Le champ due est obligatoire.",
    "needs_confirmation doit etre true pour create_action, update_action, move_action et delete_action.",
    "needs_confirmation peut etre false pour show_agenda, show_settings, load_week, analyze_week, week_offset et current_week.",
    "Statuts autorises: not_started, in_progress, done, canceled.",
    "Categories autorisees: EMPLOI, PROJET_PROFESSIONNEL, CULTURE_SPORT_LOISIRS, CITOYENNETE, FORMATION, LOGEMENT, SANTE.",
  ].join("\n");
}

function deterministicSuggest(message, appState) {
  const normalized = normalizeText(message);
  if (!normalized) return null;

  if (normalized.includes("semaine suivante") || normalized.includes("prochaine semaine")) {
    return {
      ok: true,
      command: { type: "week_offset", offset: 1 },
      explanation: "J'affiche la semaine suivante.",
      needs_confirmation: false,
    };
  }
  if (normalized.includes("semaine precedente") || normalized.includes("semaine avant")) {
    return {
      ok: true,
      command: { type: "week_offset", offset: -1 },
      explanation: "J'affiche la semaine precedente.",
      needs_confirmation: false,
    };
  }

  if (!isCreateIntent(normalized)) return null;

  const due = firstExplicitDate(message) || relativeDateFromMessage(normalized, appState);
  const title = cleanCreateTitle(message);
  if (!due && !title) {
    return {
      ok: false,
      command: null,
      explanation: "",
      error: "Donne-moi un titre et une date pour creer l'action.",
      needs_confirmation: true,
    };
  }
  if (!due) {
    return {
      ok: false,
      command: null,
      explanation: "",
      error: `Pour quelle date veux-tu creer "${title}" ?`,
      needs_confirmation: true,
    };
  }
  if (!title || isWeakTitle(title)) {
    return {
      ok: false,
      command: null,
      explanation: "",
      error: `J'ai la date (${due}). Quel titre veux-tu donner a l'action ?`,
      needs_confirmation: true,
    };
  }

  return {
    ok: true,
    command: {
      type: "create_action",
      title,
      comment: "",
      due,
      qualification: "EMPLOI",
    },
    explanation: `Je te propose de creer l'action "${title}" pour le ${due}.`,
    needs_confirmation: true,
  };
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .trim();
}

function isCreateIntent(normalized) {
  return /\b(creer|cree|creation|ajoute|ajouter|nouvelle action)\b/.test(normalized);
}

function firstExplicitDate(value) {
  const text = String(value || "");
  const iso = text.match(/\b(20\d{2}-\d{2}-\d{2})\b/);
  if (iso) return iso[1];
  const french = text.match(/\b(\d{1,2})[\/.-](\d{1,2})[\/.-](\d{2,4})\b/);
  if (!french) return "";
  const day = french[1].padStart(2, "0");
  const month = french[2].padStart(2, "0");
  const year = french[3].length === 2 ? `20${french[3]}` : french[3];
  return `${year}-${month}-${day}`;
}

function relativeDateFromMessage(normalized, appState) {
  const offsets = {
    lundi: 0,
    mardi: 1,
    mercredi: 2,
    jeudi: 3,
    vendredi: 4,
    samedi: 5,
    dimanche: 6,
  };
  for (const [day, offset] of Object.entries(offsets)) {
    if (normalized.includes(day)) return addDaysIso(appState.weekStart, offset);
  }
  return "";
}

function cleanCreateTitle(value) {
  return String(value || "")
    .replace(/\b(20\d{2}-\d{2}-\d{2})\b/g, "")
    .replace(/\b\d{1,2}[\/.-]\d{1,2}[\/.-]\d{2,4}\b/g, "")
    .replace(/\b(creer|cree|crée|creation|ajoute|ajouter|nouvelle|action|pour|stp|svp|dimanche|lundi|mardi|mercredi|jeudi|vendredi|samedi|aujourd'hui|aujourd hui|demain|un|une|de|du|des)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function isWeakTitle(value) {
  const normalized = normalizeText(value);
  return normalized.length < 3 || new Set(["tache", "truc", "action", "un", "une"]).has(normalized);
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
  if (typeof result.choices?.[0]?.message?.content === "string") return result.choices[0].message.content;
  if (typeof result.choices?.[0]?.message?.reasoning === "string") return result.choices[0].message.reasoning;
  if (typeof result.choices?.[0]?.message?.reasoning_content === "string") return result.choices[0].message.reasoning_content;
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

function normalizeAiCommand(value, appState) {
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

  const safeCommand = coerceCommand(command, appState);
  return {
    ok: true,
    command: safeCommand,
    explanation: typeof value.explanation === "string" ? value.explanation : "",
    needs_confirmation: needsConfirmation(safeCommand.type),
  };
}

function needsConfirmation(type) {
  return new Set(["move_action", "update_action", "create_action", "delete_action"]).has(type);
}

function coerceCommand(command, appState) {
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
  if (copy.type === "create_action") {
    copy.due = resolveRelativeDue(copy.due, appState);
    copy.qualification = allowedQualification(copy.qualification) ? copy.qualification : "EMPLOI";
  }
  return copy;
}

function allowedQualification(value) {
  return new Set([
    "EMPLOI",
    "PROJET_PROFESSIONNEL",
    "CULTURE_SPORT_LOISIRS",
    "CITOYENNETE",
    "FORMATION",
    "LOGEMENT",
    "SANTE",
  ]).has(value);
}

function resolveRelativeDue(value, appState) {
  if (typeof value !== "string") return "";
  if (/^20\d{2}-\d{2}-\d{2}$/.test(value)) return value;
  const normalized = value
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
  const offsets = {
    lundi: 0,
    mardi: 1,
    mercredi: 2,
    jeudi: 3,
    vendredi: 4,
    samedi: 5,
    dimanche: 6,
  };
  for (const [day, offset] of Object.entries(offsets)) {
    if (normalized.includes(day)) return addDaysIso(appState.weekStart, offset);
  }
  return value;
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
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}
