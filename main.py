import os
import json
import requests
from datetime import datetime
import anthropic
import time
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
PRODUCTION_DB = "335f30c4-acd5-81f3-9053-cf33e6c9b342"
JON_AI_OFFICE = "333f30c4-acd5-807a-929b-c1bc4d7d46de"

# Cache dinamico de clientes — se llena en runtime, no hardcodeado
_client_db_cache = {}

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
    data = r.json()
    if r.status_code != 200:
        raise Exception(f"Notion error {r.status_code}: {data.get('message', data)}")
    return data

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
    cl = cliente.lower().strip()

    # Buscar en cache primero
    for key, val in _client_db_cache.items():
        if key in cl or cl in key:
            return val

    # Si no está en cache, buscar en Notion
    r = requests.get(
        f"https://api.notion.com/v1/blocks/{JON_AI_OFFICE}/children?page_size=50",
        headers=NOTION_HEADERS
    )
    blocks = r.json().get("results", [])
    for block in blocks:
        if block["type"] != "child_page":
            continue
        page_title = block.get("child_page", {}).get("title", "").lower()
        if cl in page_title or page_title in cl:
            page_id = block["id"]
            # Buscar la DB de Videos dentro de esa página
            children = requests.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=NOTION_HEADERS
            ).json().get("results", [])
            for child in children:
                if child["type"] == "child_database":
                    db_id = child["id"]
                    _client_db_cache[page_title] = db_id
                    return db_id
    return None


def onboard_client(nombre, rubro, notas=""):
    # 1. Crear página del cliente en JON AI Office
    page = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json={
        "parent": {"page_id": JON_AI_OFFICE},
        "properties": {
            "title": {"title": [{"text": {"content": nombre}}]}
        }
    }).json()
    page_id = page.get("id")
    if not page_id:
        return {"error": "No se pudo crear la página del cliente"}

    # 2. Crear DB de Videos dentro de esa página
    db = requests.post("https://api.notion.com/v1/databases", headers=NOTION_HEADERS, json={
        "parent": {"page_id": page_id},
        "title": [{"text": {"content": "Videos"}}],
        "properties": {
            "Video":           {"title": {}},
            "Estado":          {"select": {"options": [
                {"name": "Pendiente",   "color": "red"},
                {"name": "En proceso",  "color": "yellow"},
                {"name": "Terminado",   "color": "blue"},
                {"name": "Entregado",   "color": "green"}
            ]}},
            "Urgencia":        {"select": {"options": [
                {"name": "Alta",  "color": "red"},
                {"name": "Media", "color": "yellow"},
                {"name": "Baja",  "color": "blue"}
            ]}},
            "Tarea pendiente": {"rich_text": {}},
            "Fecha limite":    {"date": {}},
            "Link del video":  {"url": {}},
            "Notas":           {"rich_text": {}},
        }
    }).json()
    db_id = db.get("id")
    if not db_id:
        return {"error": "No se pudo crear la DB de Videos"}

    # Guardar en cache
    _client_db_cache[nombre.lower()] = db_id

    # 3. Agregar entrada en DB unificada de producción
    requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json={
        "parent": {"database_id": PRODUCTION_DB},
        "properties": {
            "Tarea":    {"title": [{"text": {"content": f"{nombre} — onboarding"}}]},
            "Cliente":  {"select": {"name": nombre}},
            "Estado":   {"select": {"name": "Pendiente"}},
            "Urgencia": {"select": {"name": "Alta"}},
            "Tipo":     {"select": {"name": "Tarea"}},
            "Detalles": {"rich_text": [{"text": {"content": f"Rubro: {rubro}. {notas}".strip()}}]},
        }
    })

    return {
        "success": True,
        "cliente": nombre,
        "page_id": page_id,
        "videos_db_id": db_id,
        "mensaje": f"Cliente {nombre} creado en Notion con carpeta y DB de Videos lista."
    }


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


def add_transaction(concepto, tipo, monto, estado, cliente="", categoria="Cobro cliente", notas="", fecha=None):
    date_str = fecha if fecha else datetime.now().strftime("%Y-%m-%d")
    props = {
        "Concepto":  {"title": [{"text": {"content": concepto}}]},
        "Tipo":      {"select": {"name": tipo}},
        "Monto":     {"number": float(monto)},
        "Fecha":     {"date": {"start": date_str}},
        "Cliente":   {"rich_text": [{"text": {"content": cliente}}]},
        "Categoria": {"select": {"name": categoria}},
        "Estado":    {"select": {"name": estado}},
        "Notas":     {"rich_text": [{"text": {"content": notas}}]},
    }
    try:
        result = notion_create(FINANCE_DB, props)
        return {"success": True, "id": result.get("id"), "concepto": concepto, "monto": monto, "fecha": date_str}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
    PROPOSAL_SYSTEM = """Sos el estratega creativo de Jon AI, una agencia de contenido con IA.
Redactás propuestas comerciales en Notion. Cada propuesta tiene bloques tipados.

ESTILO Y VOZ:
- Tono: directo, estratégico, con autoridad. Nunca genérico.
- Español rioplatense: "vos", frases cortas como golpes. "No es X. Es Y."
- NUNCA "hacemos videos" — siempre "infraestructura de ventas", "activo estratégico", "sistema de conversión"
- Real estate = percepción y deseo / E-commerce = conversión y autoridad / Otros = escala y velocidad

FRASES CLAVE (usá variantes de estas):
- "El diferencial no está en el producto. Está en cómo se comunica."
- "El video no es contenido creativo. Es infraestructura de ventas."
- "No basta con mostrar. Hay que traducir en deseo."
- "La IA no reemplaza el guión. Lo ejecuta con mayor nivel."
- "No se trata de hacer videos. Se trata de profesionalizar la comunicación."

FORMATO DE SALIDA — array JSON de bloques. Cada bloque tiene "type" y "text":
- "h2": título de sección principal
- "h3": subtítulo dentro de sección (ej: nombre de fase, nombre de producto)
- "p": párrafo corto (máximo 1-2 oraciones por bloque — frases cortas, cada idea su bloque)
- "bullet": ítem de lista con viñeta
- "divider": separador (sin text)

REGLA MÁS IMPORTANTE: NO uses párrafos largos. Cada oración corta = su propio bloque "p".
Igual que en esta propuesta real de ejemplo:
[
  {"type":"p","text":"La operación no es emergente."},
  {"type":"p","text":"Es liderazgo consolidado."},
  {"type":"bullet","text":"Tienda oficial activa en Mercado Libre"},
  {"type":"bullet","text":"Posicionamiento fuerte en su categoría"},
  {"type":"p","text":"El diferencial no está en el producto."},
  {"type":"p","text":"Está en cómo se comunica."}
]"""

    total_ff = precio_ff * cantidad_videos
    total_mercado = precio_mercado * cantidad_videos

    prompt = f"""Generá la propuesta completa para este cliente. Devolvé SOLO el array JSON, sin markdown, sin texto extra.

DATOS DEL CLIENTE:
- Cliente: {cliente}
- Rubro: {rubro}
- Producto/Servicio: {producto}
- Objetivo: {objetivo}
- Cantidad de videos: {cantidad_videos}
- Precio F&F: ${precio_ff} USD/video (Total: ${total_ff} USD)
- Precio mercado: ${precio_mercado} USD/video (Total: ${total_mercado} USD)
- Detalles: {detalles if detalles else "ninguno"}

ESTRUCTURA REQUERIDA (en este orden):
1. Bloque h3 con el subtítulo estratégico del proyecto
2. divider
3. h2 "Contexto" → párrafos cortos + bullets con la situación real del cliente
4. divider
5. h2 "Enfoque IA-first" → párrafos cortos explicando cómo la IA potencia la producción
6. divider
7. h2 "Alcance / Entregables" → h3 por tipo de video + bullets con specs (formato, duración, plataforma)
8. divider
9. h2 "Proceso y Tiempos" → h3 por etapa (ej: "Etapa 1 – Planificación (48h)") + bullets con tareas
10. divider
11. h2 "Inversión" → (NO incluyas precio aquí, solo el argumento de valor — 2-3 párrafos cortos)
12. divider
13. h2 "Escalamiento Estratégico" → párrafos cortos + bullets sobre próximos pasos y crecimiento
14. divider
15. Último p: "No se trata de hacer videos."
16. p: "Se trata de profesionalizar la comunicación."

Devolvé SOLO el array JSON:"""

    resp = client_anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4000,
        system=PROPOSAL_SYSTEM,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    block_specs = json.loads(raw.strip())

    # --- Notion block builders ---
    def rich(text, bold=False, strike=False):
        ann = {}
        if bold:
            ann["bold"] = True
        if strike:
            ann["strikethrough"] = True
        item = {"type": "text", "text": {"content": text}}
        if ann:
            item["annotations"] = ann
        return [item]

    def notion_block(spec):
        t = spec.get("type")
        text = spec.get("text", "")
        if t == "h2":
            return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich(text)}}
        if t == "h3":
            return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich(text)}}
        if t == "bullet":
            return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich(text)}}
        if t == "divider":
            return {"object": "block", "type": "divider", "divider": {}}
        # default: paragraph
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(text)}}

    blocks = [notion_block(s) for s in block_specs]

    # Append investment pricing block (structured, always consistent)
    pricing_blocks = [
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich("Inversión")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": f"{cantidad_videos} videos × USD {precio_mercado}"}, "annotations": {"strikethrough": True}}
        ]}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": f"USD {total_mercado}"}, "annotations": {"strikethrough": True}}
        ]}},
        {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich(f"💛 Valor Friends & Family – Bloque Piloto")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(f"{cantidad_videos} videos × USD {precio_ff}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(f"Total: USD {total_ff}", bold=True)}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich("Este valor aplica exclusivamente para este bloque inicial.")}},
    ]

    # Find and replace the investment h2 in blocks, or just append before escalamiento
    final_blocks = []
    skip_next_divider = False
    for b in blocks:
        # Remove the AI-generated investment section (we replace with structured one)
        props = b.get("heading_2") or b.get("heading_3") or b.get("paragraph") or b.get("bulleted_list_item") or {}
        texts = props.get("rich_text", [])
        block_text = "".join(t.get("text", {}).get("content", "") for t in texts)
        if b.get("type") == "heading_2" and "nversión" in block_text:
            final_blocks.extend(pricing_blocks)
            skip_next_divider = True
            continue
        if skip_next_divider and b.get("type") == "divider":
            skip_next_divider = False
            continue
        final_blocks.append(b)

    # If investment section wasn't in the AI output, append it
    has_investment = any(
        (b.get("heading_2") or {}).get("rich_text", [{}])[0].get("text", {}).get("content", "").find("nversión") >= 0
        for b in final_blocks if b.get("type") == "heading_2"
    )
    if not has_investment:
        final_blocks.extend(pricing_blocks)

    # Closing line
    final_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich("Jon AI Team 🚀", bold=True)}})

    page_data = {
        "parent": {"page_id": PROPOSALS_PARENT_ID},
        "properties": {
            "title": {"title": [{"text": {"content": f"Propuesta — {cliente}"}}]}
        },
        "children": final_blocks
    }

    r = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=page_data)
    result = r.json()

    if "id" in result:
        page_id = result["id"].replace("-", "")
        url = f"https://www.notion.so/{page_id}"
        return {"success": True, "url": url, "cliente": cliente}
    else:
        return {"error": result.get("message", "Error creando propuesta")}


def get_production_calendar(cliente=None, estado=None, fecha=None):
    filter_conditions = []
    if cliente:
        filter_conditions.append({"property": "Cliente", "select": {"equals": cliente}})
    if estado:
        filter_conditions.append({"property": "Estado", "select": {"equals": estado}})
    if fecha:
        filter_conditions.append({"property": "Fecha", "date": {"equals": fecha}})

    if len(filter_conditions) == 1:
        body = {"filter": filter_conditions[0]}
    elif len(filter_conditions) > 1:
        body = {"filter": {"and": filter_conditions}}
    else:
        body = {}

    results = notion_query(PRODUCTION_DB, body)
    entries = []
    for r in results:
        p = r["properties"]
        entries.append({
            "id": r["id"],
            "tarea": get_text(p.get("Tarea")),
            "cliente": get_select(p.get("Cliente")),
            "fecha": p.get("Fecha", {}).get("date", {}).get("start", "") if p.get("Fecha", {}).get("date") else "",
            "estado": get_select(p.get("Estado")),
            "urgencia": get_select(p.get("Urgencia")),
            "tipo": get_select(p.get("Tipo")),
            "link": p.get("Link video", {}).get("url") or "",
            "detalles": get_text(p.get("Detalles")),
        })
    return {"entries": entries, "total": len(entries)}


def update_production_entry(tarea, cliente=None, estado=None, link=None):
    filter_body = {"filter": {"property": "Tarea", "rich_text": {"contains": tarea}}}
    if cliente:
        filter_body = {"filter": {"and": [
            {"property": "Tarea", "rich_text": {"contains": tarea}},
            {"property": "Cliente", "select": {"equals": cliente}}
        ]}}
    results = notion_query(PRODUCTION_DB, filter_body)
    updated = []
    for r in results:
        props = {}
        if estado:
            props["Estado"] = {"select": {"name": estado}}
        if link:
            props["Link video"] = {"url": link}
        if props:
            notion_update(r["id"], props)
            updated.append(get_text(r["properties"].get("Tarea")))
    return {"updated": updated, "count": len(updated)}


def add_production_entry(tarea, cliente, fecha, tipo="Video", estado="Pendiente", urgencia="Alta", detalles=""):
    props = {
        "Tarea":    {"title": [{"text": {"content": tarea}}]},
        "Cliente":  {"select": {"name": cliente}},
        "Fecha":    {"date": {"start": fecha}},
        "Estado":   {"select": {"name": estado}},
        "Urgencia": {"select": {"name": urgencia}},
        "Tipo":     {"select": {"name": tipo}},
        "Detalles": {"rich_text": [{"text": {"content": detalles}}]},
    }
    result = notion_create(PRODUCTION_DB, props)
    return {"success": True, "id": result.get("id"), "tarea": tarea}


def run_tool(name, inputs):
    tools_map = {
        "get_finance_summary":       get_finance_summary,
        "add_transaction":           add_transaction,
        "update_transaction_status": update_transaction_status,
        "get_client_videos":         get_client_videos,
        "update_video_status":       update_video_status,
        "add_video":                 add_video,
        "create_proposal":           create_proposal,
        "get_production_calendar":   get_production_calendar,
        "update_production_entry":   update_production_entry,
        "add_production_entry":      add_production_entry,
        "onboard_client":            onboard_client,
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
                "notas":     {"type": "string"},
                "fecha":     {"type": "string", "description": "Fecha en formato YYYY-MM-DD. Si no se da, usa hoy."}
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
        "name": "onboard_client",
        "description": "Crea un cliente nuevo en Notion: página, DB de Videos y entrada en producción. Usá cuando Juan diga 'nuevo cliente' o 'onboarding para X'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del cliente"},
                "rubro":  {"type": "string", "description": "Rubro o industria del cliente"},
                "notas":  {"type": "string", "description": "Notas adicionales opcionales"}
            },
            "required": ["nombre", "rubro"]
        }
    },
    {
        "name": "get_production_calendar",
        "description": "Consulta el calendario de producción unificado. Filtrá por cliente, estado o fecha.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cliente": {"type": "string", "description": "Nombre exacto del cliente. Ej: Altier, Cecchini"},
                "estado":  {"type": "string", "enum": ["Pendiente", "En proceso", "Terminado", "Entregado"]},
                "fecha":   {"type": "string", "description": "Fecha exacta en formato YYYY-MM-DD"}
            }
        }
    },
    {
        "name": "update_production_entry",
        "description": "Actualiza el estado y/o link de una entrada del calendario de producción.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tarea":   {"type": "string", "description": "Nombre o parte del nombre de la tarea"},
                "cliente": {"type": "string", "description": "Cliente para filtrar mejor"},
                "estado":  {"type": "string", "enum": ["Pendiente", "En proceso", "Terminado", "Entregado"]},
                "link":    {"type": "string", "description": "URL del video entregado"}
            },
            "required": ["tarea"]
        }
    },
    {
        "name": "add_production_entry",
        "description": "Agrega una nueva entrada al calendario de producción.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tarea":    {"type": "string"},
                "cliente":  {"type": "string"},
                "fecha":    {"type": "string", "description": "Formato YYYY-MM-DD"},
                "tipo":     {"type": "string", "enum": ["Video", "Tarea"]},
                "estado":   {"type": "string", "enum": ["Pendiente", "En proceso", "Terminado", "Entregado"]},
                "urgencia": {"type": "string", "enum": ["Alta", "Media", "Baja"]},
                "detalles": {"type": "string"}
            },
            "required": ["tarea", "cliente", "fecha"]
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
Tu trabajo es gestionar el Notion de la empresa: clientes, videos, producción y finanzas.

EQUIPO:
- Juan: CEO, ventas, finanzas, dirección creativa (50%)
- Martiniano: operativo, producción y equipo (25%)
- Camilao: operativo, producción y equipo (25%)
Sociedad activa desde abril 2026. Distribución mensual: revenue - gastos = ganancia neta, 10% fondo emergencia, resto 50/25/25.

CLIENTES:
No tenés una lista fija de clientes. Siempre usá los tools para buscarlos y verificarlos.
Nunca respondas "no encuentro ese cliente" sin antes haber intentado llamar a get_client_videos o add_video —
el tool mismo te va a decir si existe o no. Si el cliente se acaba de crear (con onboard_client),
ya existe en Notion aunque no lo hayas visto antes.

CALENDARIO DE PRODUCCIÓN:
Hay una DB unificada con todos los videos y tareas por fecha. Podés consultar, actualizar estado,
agregar links y crear nuevas entradas. Cuando alguien diga "entregué X", "el video de Y está listo",
"poné el link de Z" — usá update_production_entry. Para ver qué hay pendiente usá get_production_calendar.

FINANZAS:
Cuando digan "Cecchini pagó", "agregar cobro", "¿cuánto nos deben?" — usá las tools financieras.

PROPUESTAS:
Cuando digan "haceme una propuesta para X" — pedí los datos que falten y usá create_proposal.

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
            # Limpiar historial para evitar que Claude omita tool calls en el próximo request
            conversation_history[user_id] = []
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
    print("Esperando 15s para que instancia anterior cierre...", flush=True)
    time.sleep(15)
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot corriendo...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
