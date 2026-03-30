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

    return {
        "ingresos_cobrados": round(cobrado, 2),
        "ingresos_pendientes": round(pendiente, 2),
        "egresos": round(egresos, 2),
        "ganancia_neta_cobrado": round(cobrado - egresos, 2),
        "ganancia_si_cobras_todo": round(cobrado + pendiente - egresos, 2),
        "detalle_pendientes": detalles_pendientes
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


def run_tool(name, inputs):
    tools_map = {
        "get_finance_summary":      get_finance_summary,
        "add_transaction":          add_transaction,
        "update_transaction_status": update_transaction_status,
        "get_client_videos":        get_client_videos,
        "update_video_status":      update_video_status,
        "add_video":                add_video,
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

Respondé siempre en español, corto y directo. Confirmá los cambios realizados."""


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    await update.message.reply_text("⏳")

    messages = [{"role": "user", "content": user_message}]

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
    app.run_polling()


if __name__ == "__main__":
    main()
