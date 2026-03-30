import os
import json
import requests
from datetime import datetime
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

FINANCE_DB = "333f30c4-acd5-819c-9c2e-d796c93644c1"
PROPOSALS_PARENT_ID = "333f30c4-acd5-80bd-96d2-c2c603681972"

CLIENT_VIDEO_DBS = {
    "cecchini":     "333f30c4-acd5-8168-af35-d4a14e4c3a60",
    "gran 28":      "333f30c4-acd5-81c2-b942-fc60bc2a43ee",
    "gran28":       "333f30c4-acd5-81c2-b942-fc60bc2a43ee",
    "altier":       "333f30c4-acd5-8118-a4db-c307331c4115",
    "integra":      "333f30c4-acd5-81b5-ab58-d35f3dfca3c1",
    "wgw":          "333f30c4-acd5-8137-99c1-fc81f85f098c",
    "la galera":    "333f30c4-acd5-81dd-90f0-dc02e08d09c1",
    "real billion": "333f30c4-acd5-8199-b0a6-e860bc1a7189",
    "andenia":      "333f30c4-acd5-815b-bb6c-db66ffd7c0e5",
    "ocha":         "333f30c4-acd5-814b-8fc7-cec404b978a0",
    "luqstoff":     "333f30c4-acd5-8126-a203-cab9b6a9b790",
    "acrule":       "333f30c4-acd5-81de-a4db-ed547676d17f",
    "cascara":      "333f30c4-acd5-8185-bc4e-d3422648a1c9",
    "lavenue":      "333f30c4-acd5-8117-a171-f5ce23fc8ab4",
    "l'avenue":     "333f30c4-acd5-8117-a171-f5ce23fc8ab4",
    "avenue":       "333f30c4-acd5-8117-a171-f5ce23fc8ab4",
}

client_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# --- Notion helpers ---

def notion_query(db_id, filter_body=None):
    body = filter_body or {}
    r = requests.post(f"https://api.notion.com/v1/databases/{db_id}/query",
                      headers=NOTION_HEADERS, json=body)
    return r.json().get("results", [])

def notion_create(db_id, properties):
    r = requests.post("https://api.notion.com/v1/pages",
                      headers=NOTION_HEADERS,
                      json={"parent": {"database_id": db_id}, "properties": properties})
    return r.json()

def notion_update(page_id, properties):
    r = requests.patch(f"https://api.notion.com/v1/pages/{page_id}",
                       headers=NOTION_HEADERS, json={"properties": properties})
    return r.json()

def get_text(prop):
    if not prop:
        return ""
    t = prop.get("type")
    items = prop.get("title", []) if t == "title" else prop.get("rich_text", [])
    return "".join(i.get("plain_text", "") for i in items)

def get_select(prop):
    if not prop:
        return ""
    s = prop.get("select")
    return s.get("name", "") if s else ""

def get_number(prop):
    return prop.get("number") or 0 if prop else 0

def find_client_db(cliente):
    cl = cliente.lower()
    for key, val in CLIENT_VIDEO_DBS.items():
        if key in cl or cl in key:
            return val
    return None


# --- Tool implementations ---

def get_finance_summary(month=None):
    results = notion_query(FINANCE_DB)
    cobrado = pendiente = egresos = 0
    detalles_pendientes = []
    detalle_egresos = []

    for r in results:
        props = r["properties"]
        tipo   = get_select(props.get("Tipo"))
        estado = get_select(props.get("Estado"))
        monto  = get_number(props.get("Monto"))
        fecha_prop = props.get("Fecha", {}).get("date")

        if month and fecha_prop:
            if not fecha_prop.get("start", "").startswith(month):
                continue

        if tipo == "Ingreso":
            if estado == "Pendiente":
                pendiente += monto
                cliente = get_text(props.get("Cliente"))
                concepto = get_text(props.get("Concepto"))
                detalles_pendientes.append(f"{cliente} - {concepto}: ${monto}")
            else:
                cobrado += monto
        elif tipo == "Egreso":
            egresos += monto
            concepto = get_text(props.get("Concepto"))
            categoria = get_select(props.get("Categoria"))
            detalle_egresos.append(f"{concepto} ({categoria}): ${monto}")

    return {
        "ingresos_cobrados": round(cobrado, 2),
        "ingresos_pendientes": round(pendiente, 2),
        "egresos": round(egresos, 2),
        "ganancia_neta_cobrado": round(cobrado - egresos, 2),
        "ganancia_si_cobras_todo": round(cobrado + pendiente - egresos, 2),
        "detalle_pendientes": detalles_pendientes,
        "detalle_egresos": detalle_egresos
    }


def add_transaction(concepto, tipo, monto, estado, cliente="", categoria="Cobro cliente", notas=""):
    today = datetime.now().strftime("%Y-%m-%d")
    props = {
        "Concepto":  {"title": [{"text": {"content": concepto}}]},
        "Tipo":      {"select": {"name": tipo}},
        "Monto":     {"number": float(monto)},
        "Fecha":     {"date": {"start": today}},
        "Cliente":   {"rich_text": [{"text": {"content": cliente}}]},
        "Categoria": {"select": {"name": categoria}},
        "Estado":    {"select": {"name": estado}},
        "Notas":     {"rich_text": [{"text": {"content": notas}}]},
    }
    result = notion_create(FINANCE_DB, props)
    return {"success": True, "id": result.get("id")}


def update_transaction_status(cliente, new_estado, concepto=None):
    results = notion_query(FINANCE_DB, {
        "filter": {
            "and": [
                {"property": "Cliente", "rich_text": {"contains": cliente}},
                {"property": "Estado",  "select":    {"equals": "Pendiente"}}
            ]
        }
    })
    updated = []
    for r in results:
        nombre = get_text(r["properties"].get("Concepto"))
        if concepto and concepto.lower() not in nombre.lower():
            continue
        notion_update(r["id"], {"Estado": {"select": {"name": new_estado}}})
        updated.append(nombre)
    return {"updated": updated, "count": len(updated)}


def get_client_videos(cliente):
    db_id = find_client_db(cliente)
    if not db_id:
        return {"error": f"Cliente '{cliente}' no encontrado"}
    results = notion_query(db_id)
    videos = []
    for r in results:
        props = r["properties"]
        videos.append({
            "nombre":  get_text(props.get("Video")),
            "estado":  get_select(props.get("Estado")),
            "urgencia": get_select(props.get("Urgencia")),
        })
    return {"cliente": cliente, "videos": videos, "total": len(videos)}


def update_video_status(cliente, estado, video_name=None, link=None):
    db_id = find_client_db(cliente)
    if not db_id:
        return {"error": f"Cliente '{cliente}' no encontrado"}
    results = notion_query(db_id)
    updated = []
    for r in results:
        nombre = get_text(r["properties"].get("Video"))
        if video_name and video_name.lower() not in nombre.lower():
            continue
        props = {"Estado": {"select": {"name": estado}}}
        if link:
            props["Link del video"] = {"url": link}
        notion_update(r["id"], props)
        updated.append(nombre)
    return {"updated": updated, "count": len(updated)}


def add_video(cliente, video_name, estado="Pendiente", urgencia="Alta", tarea="", notas=""):
    db_id = find_client_db(cliente)
    if not db_id:
        return {"error": f"Cliente '{cliente}' no encontrado"}
    props = {
        "Video":           {"title": [{"text": {"content": video_name}}]},
        "Estado":          {"select": {"name": estado}},
        "Urgencia":        {"select": {"name": urgencia}},
        "Tarea pendiente": {"rich_text": [{"text": {"content": tarea}}]},
        "Notas":           {"rich_text": [{"text": {"content": notas}}]},
    }
    result = notion_create(db_id, props)
    return {"success": True, "video": video_name}


def create_proposal(cliente, rubro, producto, objetivo, cantidad_videos, precio_ff, precio_mercado, detalles=""):
    PROPOSAL_SYSTEM = """Sos el estratega creativo de Jon AI, una agencia de contenido con IA. Tu trabajo es redactar propuestas comerciales para clientes potenciales.

ESTILO Y VOZ:
- Tono: directo, estratégico, con autoridad. Nunca genérico.
- Lenguaje: español rioplatense. "vos", "no es X, es Y", frases cortas como golpes.
- NUNCA decir "hacemos videos". El contenido es infraestructura, activo estratégico, herramienta de conversión.
- Real estate = percepción y deseo / E-commerce = conversión y autoridad / Otros = escala y velocidad.

FRASES CLAVE POR INDUSTRIA:
Real estate: "No basta con mostrar espacios. Hay que traducir arquitectura en deseo." / "En real estate, la percepción visual define el nivel del desarrollador."
E-commerce: "El video no es contenido creativo. Es infraestructura de ventas." / "El diferencial no está en el producto. Está en cómo se comunica."
General: "La IA no reemplaza el guión. Lo ejecuta con mayor nivel." / "No se trata de hacer videos. Se trata de profesionalizar la comunicación."

Cada sección debe ser densa, específica, con argumentos concretos. Sin paja, sin frases vacías."""

    total_ff = precio_ff * cantidad_videos
    total_mercado = precio_mercado * cantidad_videos

    prompt = f"""Generá una propuesta completa para este cliente de Jon AI.

DATOS:
- Cliente: {cliente}
- Rubro: {rubro}
- Producto/Servicio: {producto}
- Objetivo del contenido: {objetivo}
- Cantidad de videos: {cantidad_videos}
- Precio Friends & Family: ${precio_ff} USD/video → Total: ${total_ff} USD
- Precio real de mercado: ${precio_mercado} USD/video → Total: ${total_mercado} USD
- Detalles adicionales: {detalles if detalles else "ninguno"}

Devolvé SOLO un JSON válido, sin markdown, sin texto antes ni después:
{{
  "subtitulo": "Una línea corta y contundente que define el proyecto estratégicamente. Ej: 'Sistema de Video UGC para Conversión en Meta Ads'",
  "contexto": "3-4 oraciones que describen la situación actual del cliente. Qué tienen hoy, qué les falta, por qué el contenido es urgente ahora para su industria. Sin suavizar la realidad.",
  "enfoque_ia": "2-3 oraciones que explican cómo la IA acelera y potencia la producción. Qué se puede hacer con IA que no se podría sin ella: velocidad de iteración, escala, calidad consistente.",
  "alcance": "Lista precisa de los {cantidad_videos} videos: formato (UGC, testimonial, demo, etc.), duración estimada, plataforma destino, tipo de guión, si incluye locución o no.",
  "proceso": "3 fases concretas con tiempos en horas hábiles. Fase 1: briefing y guiones (X hs). Fase 2: producción IA (X hs). Fase 3: entrega + feedback (X hs). Incluir Drive compartido y rondas de revisión.",
  "inversion": "Presentá el precio de mercado de ${total_mercado} USD como referencia de valor real, y el precio F&F de ${total_ff} USD como el precio del proyecto. Justificá por qué ese precio es una ventaja estratégica ahora.",
  "escalabilidad": "2-3 oraciones sobre qué viene después: más videos, más formatos, más plataformas. Cómo este proyecto se convierte en un sistema de contenido permanente para el cliente."
}}"""

    resp = client_anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        system=PROPOSAL_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    data = json.loads(raw.strip())

    def rich(text):
        return [{"type": "text", "text": {"content": text}}]

    def rich_strikethrough(text):
        return [{"type": "text", "text": {"content": text}, "annotations": {"strikethrough": True}}]

    def h2(text):
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich(text)}}

    def h3(text):
        return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich(text)}}

    def para(text):
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}

    def para_mixed(parts):
        # parts = list of (text, strikethrough bool)
        rt = []
        for text, strike in parts:
            item = {"type": "text", "text": {"content": text}}
            if strike:
                item["annotations"] = {"strikethrough": True}
            rt.append(item)
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt}}

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    inversion_block = para_mixed([
        (f"Precio real de mercado: ${total_mercado} USD", True),
        (f"  →  Precio Friends & Family: ${total_ff} USD\n\n", False),
        (data["inversion"], False),
    ])

    blocks = [
        h3(data["subtitulo"]),
        divider(),
        h2("Contexto"),
        para(data["contexto"]),
        divider(),
        h2("Enfoque IA-first"),
        para(data["enfoque_ia"]),
        divider(),
        h2("Alcance / Entregables"),
        para(data["alcance"]),
        divider(),
        h2("Proceso y tiempos"),
        para(data["proceso"]),
        divider(),
        h2("Inversión"),
        inversion_block,
        divider(),
        h2("Escalabilidad"),
        para(data["escalabilidad"]),
        divider(),
        para("Jon AI Team 🚀"),
    ]

    page_data = {
        "parent": {"page_id": PROPOSALS_PARENT_ID},
        "properties": {
            "title": {"title": [{"text": {"content": f"Propuesta — {cliente}"}}]}
        },
        "children": blocks
    }

    r = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=page_data)
    result = r.json()

    if "id" in result:
        page_id = result["id"].replace("-", "")
        url = f"https://www.notion.so/{page_id}"
        return {"success": True, "url": url, "cliente": cliente}
    else:
        return {"error": result.get("message", "Error creando propuesta")}


def run_tool(name, inputs):
    tools_map = {
        "get_finance_summary":      get_finance_summary,
        "add_transaction":          add_transaction,
        "update_transaction_status": update_transaction_status,
        "get_client_videos":        get_client_videos,
        "update_video_status":      update_video_status,
        "add_video":                add_video,
        "create_proposal":          create_proposal,
    }
    fn = tools_map.get(name)
    return fn(**inputs) if fn else {"error": "Tool not found"}


# --- Claude tools schema ---

TOOLS = [
    {
        "name": "get_finance_summary",
        "description": "Obtiene resumen financiero: ingresos cobrados, pendientes, egresos, ganancia neta.",
        "input_schema": {
            "type": "object",
            "properties": {
                "month": {"type": "string", "description": "Mes en formato YYYY-MM, ej: '2026-03'. Opcional."}
            }
        }
    },
    {
        "name": "add_transaction",
        "description": "Agrega una nueva transacción financiera (ingreso o egreso) en Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concepto":  {"type": "string"},
                "tipo":      {"type": "string", "enum": ["Ingreso", "Egreso"]},
                "monto":     {"type": "number"},
                "estado":    {"type": "string", "enum": ["Cobrado", "Pagado", "Pendiente"]},
                "cliente":   {"type": "string"},
                "categoria": {"type": "string", "enum": ["Cobro cliente", "Herramientas / Software", "Sueldos", "Marketing", "Impuestos", "Otro"]},
                "notas":     {"type": "string"}
            },
            "required": ["concepto", "tipo", "monto", "estado"]
        }
    },
    {
        "name": "update_transaction_status",
        "description": "Marca transacciones pendientes de un cliente como cobradas o pagadas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente":    {"type": "string"},
                "new_estado": {"type": "string", "enum": ["Cobrado", "Pagado"]},
                "concepto":   {"type": "string", "description": "Filtro opcional por concepto"}
            },
            "required": ["cliente", "new_estado"]
        }
    },
    {
        "name": "get_client_videos",
        "description": "Obtiene el estado de los videos de un cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente": {"type": "string"}
            },
            "required": ["cliente"]
        }
    },
    {
        "name": "update_video_status",
        "description": "Actualiza el estado de uno o todos los videos de un cliente. Puede agregar link.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente":    {"type": "string"},
                "estado":     {"type": "string", "enum": ["Pendiente", "En proceso", "Entregado"]},
                "video_name": {"type": "string", "description": "Nombre del video. Si no se da, actualiza todos."},
                "link":       {"type": "string"}
            },
            "required": ["cliente", "estado"]
        }
    },
    {
        "name": "add_video",
        "description": "Agrega un nuevo video a la lista de un cliente.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente":    {"type": "string"},
                "video_name": {"type": "string"},
                "estado":     {"type": "string", "enum": ["Pendiente", "En proceso", "Entregado"]},
                "urgencia":   {"type": "string", "enum": ["Alta", "Media", "Baja"]},
                "tarea":      {"type": "string"},
                "notas":      {"type": "string"}
            },
            "required": ["cliente", "video_name"]
        }
    },
    {
        "name": "create_proposal",
        "description": "Genera una propuesta comercial completa en Notion para un cliente potencial, con voz y estructura de Jon AI.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente":          {"type": "string", "description": "Nombre del cliente o empresa"},
                "rubro":            {"type": "string", "description": "Industria o rubro del cliente. Ej: real estate, e-commerce, skincare"},
                "producto":         {"type": "string", "description": "Qué producto o servicio vende el cliente"},
                "objetivo":         {"type": "string", "description": "Qué quiere lograr con el contenido. Ej: conversión, ads, posicionamiento"},
                "cantidad_videos":  {"type": "integer", "description": "Cantidad de videos del proyecto"},
                "precio_ff":        {"type": "number", "description": "Precio Friends & Family por video en USD"},
                "precio_mercado":   {"type": "number", "description": "Precio real de mercado por video en USD (se muestra tachado)"},
                "detalles":         {"type": "string", "description": "Cualquier detalle extra del proyecto. Opcional."}
            },
            "required": ["cliente", "rubro", "producto", "objetivo", "cantidad_videos", "precio_ff", "precio_mercado"]
        }
    }
]

SYSTEM_PROMPT = """Sos el asistente de Jon AI, una agencia de contenido UGC con IA.
Tu trabajo es gestionar el Notion de la empresa: clientes, videos y finanzas.

Clientes activos: Cecchini (marroquinería), Gran 28 (impresoras laser / máquinas de tatuar),
Altiér (crema celulitis), Íntegra (barras de proteína), WGW (real state),
La Galera (real state), Real Billion (real state), Andenia (fintech),
Ocha (real state), Luqstoff (electrodomésticos), Acrule (hidroponía),
Cáscara (agencia marketing), L'Avenue (real state - en negociación).

Cuando el usuario te diga algo como "Cecchini pagó todo", "entregué el video 3 de Luqstoff",
"¿cuánto me deben?", "agregá un video a Gran 28" — interpretá la intención y usá las herramientas.

También podés generar propuestas comerciales en Notion. Cuando el usuario diga algo como
"haceme una propuesta para X", "generá propuesta para tal cliente", pedile los datos que falten
(rubro, producto, objetivo, cantidad de videos, precio F&F, precio mercado) y usá create_proposal.

Respondé siempre en español, corto y directo. Confirmá los cambios realizados."""


# Historial de conversacion por usuario (en memoria)
conversation_history = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.message.from_user.id
    await update.message.reply_text("⏳")

    # Recuperar historial o crear nuevo
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({"role": "user", "content": user_message})

    # Limitar historial a los ultimos 20 mensajes para no pasarse de tokens
    messages = conversation_history[user_id][-20:]

    while True:
        response = client_anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            text = "".join(b.text for b in response.content if hasattr(b, "text"))
            await update.message.reply_text(text or "Listo.")
            # Solo guardar texto final en historial
            if text:
                conversation_history[user_id].append({"role": "assistant", "content": text})
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_use_id" if False else "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
