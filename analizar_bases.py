#!/usr/bin/env python3
"""
analizar_bases.py — Resumen orientativo de los documentos de un proceso con Gemini (bases, absolución de consultas, otros).
"""
import argparse
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

import requests

from supabase_sync import get_client, load_env

MODELO_DEFECTO = "gemini-3.8-flash"
MODELOS_RESPALDO = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]   # solo si no se puede consultar la lista de modelos
MAX_MB = 80                    # no descargar archivos mayores
PAUSA_ENTRE_PROCESOS = 8       # segundos, para no pasarse del límite por minuto
COLGADO_MIN = 30               # un 'procesando' más viejo que esto se vuelve a intentar
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

PROMPT = """Eres un asistente que ayuda a ingenieros de una consultora peruana a entender rápidamente unas BASES de una
contratación pública (consultoría de obra / supervisión) antes de que hagan su propio análisis. Hoy es {HOY}.
Lee los documentos adjuntos (pueden ser las bases y/o los términos de referencia, TDR; a veces son escaneos: lee con cuidado cifras y montos)
y responde SOLO con un JSON válido (sin texto adicional) con exactamente estas claves:

{
  "objeto_alcance": "texto breve: qué se contrata y alcance del servicio",
  "tipo_servicio": "qué servicio se contrata exactamente (ej. 'Supervisión de la elaboración del expediente técnico y de la ejecución de la obra', 'Elaboración de expediente técnico', 'Supervisión de obra')",
  "tipologia": [{"especialidad": "especialidad principal del PROYECTO (ej. Saneamiento, Vial, Edificaciones, Eléctrica)", "subespecialidades": ["subespecialidad del proyecto (ej. agua potable rural, alcantarillado, vías urbanas)"]}],
  "plazo": "plazo del servicio, total y por etapas si las hay",
  "plazo_otra_cifra": "si otra parte de las bases da un total de plazo distinto al principal, escríbelo (ej. '210 días calendario'); si no hay contradicción, 'No consta en las bases'",
  "fechas": {
    "inicio_consultas": "DD/MM/AAAA",
    "fin_consultas": "DD/MM/AAAA",
    "absolucion_consultas": "DD/MM/AAAA",
    "integracion_bases": "DD/MM/AAAA",
    "presentacion_ofertas": "DD/MM/AAAA",
    "buena_pro": "DD/MM/AAAA"
  },
  "otros_hitos": [{"hito": "nombre del hito del calendario que no está arriba (convocatoria, expresiones de interés, evaluación, suscripción de convenio o contrato, etc.)", "fecha": "DD/MM/AAAA, o 'DD/MM/AAAA al DD/MM/AAAA' si es un rango"}],
  "valor_referencial": "monto total del valor referencial tal como figura, ej. 'S/ 608,085.27' (indica si incluye IGV)",
  "limite_inferior": "monto mínimo de la oferta económica admitida, con dos decimales, ej. 'S/ 547,276.75 (90 % del valor referencial)'",
  "limite_superior": "monto máximo de la oferta económica admitida, con dos decimales, ej. 'S/ 668,893.79 (110 % del valor referencial)'",
  "regla_redondeo_limites": "nota literal de las bases sobre cómo se calculan o redondean los límites, o 'No consta en las bases'",
  "forma_pago": "cómo se calcula y cuándo se paga, tal como lo dicen las bases",
  "garantias": "garantías exigidas (fiel cumplimiento, adelantos, retención) o 'No consta'",
  "experiencia_postor": "experiencia exigida al postor: monto (con número, ej. '1 vez el valor referencial'), período en años, máximo de contrataciones o servicios con que se puede acreditar, tipo de servicios similares aceptados (resumidos) y cualquier experiencia adicional exigida",
  "personal_clave": [{"cargo": "...", "requisitos": "formación y experiencia exigidas"}],
  "certificaciones": [{"certificacion": "...", "detalle": "a quién se exige (postor o personal) y para qué"}],
  "equipamiento": [{"equipo": "...", "cantidad": "...", "detalle": "características, antigüedad, si es propio o alquilado, etc."}],
  "factores_evaluacion": [{"factor": "...", "puntaje": "...", "detalle": "..."}],
  "documentos_oferta": ["documentos y formatos obligatorios de la oferta"],
  "penalidades_y_alertas": ["hechos relevantes del documento: penalidades, causales de descalificación, contradicciones y otros requisitos importantes (sin opiniones)"]
}

Reglas:
- Usa SOLO lo que dice el documento. Si algo no aparece o no se lee con certeza, escribe "No consta en las bases" (en las listas, devuelve una lista vacía []). No inventes ni adivines cifras ni fechas.
- Fechas: pueden figurar en el calendario o cronograma del procedimiento, en la convocatoria o en los TDR. Escríbelas como DD/MM/AAAA. Agrega la hora (HH:MM, 24 h) SOLO si el documento indica una hora explícita; nunca pongas 00:00 por defecto. Si un hito es un rango, escríbelo "DD/MM/AAAA al DD/MM/AAAA".
- Cronograma: en "fechas" van los seis hitos indicados; todos los demás hitos del calendario (convocatoria, registro o presentación de expresiones de interés, evaluación de ofertas, suscripción del convenio o contrato…) van en "otros_hitos".
- Límites: copia los montos de la tabla de las bases. Si hay una nota que explique el redondeo (por ejemplo, que el límite inferior se aumenta en un dígito el segundo decimal y que el superior se toma sin redondear), cópiala literal en "regla_redondeo_limites" y aplícala a los montos, que deben quedar con dos decimales. Si las bases dan solo el porcentaje, calcula el monto y deja el porcentaje entre paréntesis. Si no constan, escribe "No consta en las bases".
- Tipología: SOLO las especialidades y subespecialidades del proyecto que se ejecuta o supervisa, según su nombre, el objeto y los TDR (por ejemplo, un proyecto de movilidad urbana con pistas y veredas es Vial → vías urbanas). Usa solo términos que figuren en el nombre, el objeto o los TDR del proyecto y no deduzcas componentes (pistas, veredas, etc.) a partir de otras listas. NO uses como tipología las listas de "obras similares" ni las de experiencia del postor o del personal (por ejemplo, "carreteras, caminos vecinales"): eso son requisitos de experiencia, no el tipo de proyecto.
- Tipo de servicio: indica si es supervisión de ejecución de obra, supervisión de la elaboración del expediente técnico, elaboración de expediente técnico u otro, o una combinación.
- Plazo: indica el total y el de cada etapa tal como figuran. Si las cifras no coinciden entre sí o entre secciones (por ejemplo, un total de 240 días pero etapas que suman 210), deja en "plazo" el total principal, escribe la otra cifra en "plazo_otra_cifra" y repórtalo en "penalidades_y_alertas".
- Forma de pago: transcribe cómo se calcula y cuándo se paga, sin suponer nada. Indica con claridad si el pago depende del avance físico, de los días o meses trabajados, de valorizaciones u otro criterio, y si las bases dicen expresamente que es independiente del avance. No uses expresiones que no estén en el documento.
- Personal clave: incluye TODO el personal de TODAS las etapas. Si las bases separan etapas (por ejemplo, elaboración del expediente técnico y ejecución de la obra), escribe la etapa entre paréntesis en el cargo, ej. "Ingeniero de Estructuras (elaboración del expediente técnico)". Si un mismo cargo tiene requisitos distintos en cada etapa, devuélvelo como filas separadas.
- Experiencia del postor: incluye siempre, si constan, el monto exigido, el período, el máximo de contrataciones o servicios con que se acredita (por ejemplo "hasta 10 contrataciones"), el tipo de servicios similares aceptados (resume la lista, no la copies entera) y la experiencia adicional exigida (por ejemplo como supervisor en 2 proyectos similares).
- No unas con "o" ni con "y" requisitos que estén en páginas distintas si las bases no los vinculan: escríbelos por separado, cada uno con su cita.
- Factores de evaluación: incluye todos los factores técnicos y, como último, la evaluación económica con su puntaje máximo y su fórmula, si constan.
- Certificaciones: certificaciones, registros o acreditaciones que se exigen (ISO, RNP, colegiatura y habilitación del personal, certificados de calibración de equipos, etc.). No incluyas aquí los documentos que acreditan equipos o locales (tarjeta de propiedad, promesas de alquiler): eso va en equipamiento.
- Equipamiento: equipos, vehículos, computadoras, software, oficina o infraestructura que la empresa (postor) debe tener o aportar para el servicio (por ejemplo, equipo topográfico, camioneta, PC). Suele estar en los TDR como "equipamiento estratégico", "equipamiento mínimo" o "infraestructura". Si se exige oficina de coordinación, local o infraestructura, inclúyela como un ítem más. No incluyas personal.
- Alertas: son HECHOS tomados del documento, cada uno con su cita; no incluyas opiniones, juicios ni valoraciones (por ejemplo "atípico", "inusual", "debería"). Incluye siempre, si constan: el puntaje técnico mínimo para pasar a la evaluación económica; el tope de las penalidades; cualquier dato de las bases que contradiga a otro (plazos, fechas, nombre o dirección de la entidad); y las causales de descalificación. Si las bases exigen un registro previo (expresión de interés, participantes), escríbelo como "Las bases exigen …" con su fecha e indica si esa fecha ya venció respecto a hoy, sin concluir quién puede o no participar.
- Indica entre paréntesis dónde lo encontraste, por ejemplo "(Cap. III, pág. 24)". Cita siempre la página del valor referencial, de los límites, del plazo, de la forma de pago, de las garantías y de cada alerta. La cita va en su propio paréntesis, al final del texto, nunca dentro de otro paréntesis ni en medio de la frase.
- Cuando menciones una norma o un artículo, usa el nombre que le dan las bases (Ley, Reglamento, etc.); no atribuyas a la Ley artículos del Reglamento.
- Sé breve y concreto. Español neutro. Cantidades y plazos tal como figuran.
"""


# Reglas que comparten los prompts de absolución y de documentos genéricos (las bases tienen las suyas, arriba)
REGLAS_COMUNES = """
Reglas generales:
- Usa SOLO lo que dice el documento. Si algo no aparece o no se lee con certeza, escribe "No consta en el documento" (en las listas, devuelve una lista vacía []). No inventes ni adivines cifras ni fechas.
- Fechas como DD/MM/AAAA. Agrega la hora (HH:MM, 24 h) SOLO si el documento indica una hora explícita; nunca pongas 00:00 por defecto. Si un hito es un rango, escríbelo "DD/MM/AAAA al DD/MM/AAAA".
- Indica entre paréntesis la página donde lo encontraste, con el formato "(pág. 12)" o "(Numeral 3.2, pág. 12)". La cita va en su propio paréntesis, al final del texto, nunca dentro de otro paréntesis ni en medio de la frase.
- Cuando menciones una norma o un artículo, usa el nombre que le da el documento (Ley, Reglamento, etc.).
- Los hechos van sin opiniones, juicios ni valoraciones (nada de «atípico», «inusual», «debería»).
- Sé breve y concreto. Español neutro. Cantidades y plazos tal como figuran.
"""

# Pliego de absolución de consultas y observaciones. Las tablas de la web muestran solo las 3 primeras claves de cada fila: tema, decisión, efecto.
PROMPT_ABSOLUCION = """Eres un asistente que ayuda a ingenieros de una consultora peruana a entender rápidamente un documento de una
contratación pública (consultoría de obra / supervisión): el PLIEGO DE ABSOLUCIÓN DE CONSULTAS Y OBSERVACIONES (o un documento equivalente). Hoy es {HOY}.
Este documento responde las consultas y observaciones que los participantes hicieron a las bases y, en varias respuestas, MODIFICA o ACLARA requisitos.
Tu trabajo es decir qué cambia respecto de las bases, sin repetir todas las consultas. A veces son escaneos: lee con cuidado cifras y montos.
Responde SOLO con un JSON válido (sin texto adicional) con exactamente estas claves:

{
  "resumen": "2 a 4 frases: qué documento es, cuántas consultas absuelve y qué cambios importantes trae",
  "total_consultas": "número de consultas y observaciones absueltas, tal como figura, o 'No consta en el documento'",
  "cambios_a_las_bases": [{"tema": "numeral o sección de las bases a la que se refiere y de qué trata (ej. 'Cap. III, 3.2: experiencia del jefe de supervisión')", "decisión": "Acogida | Acogida parcialmente | No acogida | Aclaración", "efecto": "qué queda vigente según la respuesta (requisito, plazo, monto, documento, etc.), tal como lo dice el documento (pág. N)"}],
  "aclaraciones": ["respuestas que no modifican las bases pero precisan cómo se aplica un requisito importante (pág. N)"],
  "hitos": [{"hito": "fecha o plazo que el documento fija o modifica (integración de bases, presentación de ofertas, buena pro, etc.)", "fecha": "DD/MM/AAAA, o 'DD/MM/AAAA al DD/MM/AAAA' si es un rango"}],
  "alertas": ["hechos relevantes: requisitos que se modifican, se flexibilizan o se endurecen (descríbelos sin opinar), contradicciones entre una respuesta y las bases, respuestas que remiten a una futura integración de bases y plazos que cambian (pág. N)"]
}

Reglas específicas:
- En "cambios_a_las_bases" incluye SOLO consultas cuya respuesta modifica o precisa algo que afecte al postor: experiencia del postor, personal clave, equipamiento, certificaciones, plazo, forma de pago, garantías, penalidades, factores de evaluación, documentos de la oferta, valor referencial y cronograma. Omite las de forma, las repetidas y las sin efecto.
- Ordena los cambios de más a menos importantes y devuelve como máximo 25. Si hay más, agrupa los menores en una sola fila.
- "decisión" solo puede tomar uno de los cuatro valores indicados.
- Cada texto debe entenderse sin haber leído la consulta original.
- Si el documento dice que las bases se integrarán con estas respuestas, anótalo en "alertas".
""" + REGLAS_COMUNES

# Cualquier otro documento (convocatoria, TDR, circular, acta, anexo…). Se usa «hitos» y no «fechas» a propósito: la web reserva «fechas» para el formato de bases.
PROMPT_GENERICO = """Eres un asistente que ayuda a ingenieros de una consultora peruana a entender rápidamente un documento de una
contratación pública (consultoría de obra / supervisión) antes de que hagan su propio análisis. Hoy es {HOY}.
El documento adjunto puede ser una convocatoria, unos términos de referencia (TDR), una circular, un acta, un anexo u otro documento del procedimiento.
A veces son escaneos: lee con cuidado cifras y montos.
Responde SOLO con un JSON válido (sin texto adicional) con exactamente estas claves:

{
  "resumen": "3 a 6 frases: qué es el documento y qué dice de importante",
  "tipo_documento": "qué documento es (ej. 'Convocatoria', 'Términos de referencia', 'Circular', 'Acta de otorgamiento de la buena pro')",
  "puntos_clave": ["los datos más importantes para un postor, uno por elemento, cada uno con su cita (pág. N)"],
  "hitos": [{"hito": "fecha o plazo que figura en el documento", "fecha": "DD/MM/AAAA, o 'DD/MM/AAAA al DD/MM/AAAA' si es un rango"}],
  "montos": [{"concepto": "qué monto es (valor referencial, garantía, penalidad, etc.)", "monto": "S/ 0.00 tal como figura"}],
  "alertas": ["hechos relevantes: plazos, requisitos llamativos, contradicciones y cambios respecto de otros documentos, solo si el propio documento los menciona (pág. N)"]
}

Reglas específicas:
- Resume solo lo que sirve para decidir si participar y para preparar la oferta; no copies el documento.
- Si no hay hitos, montos o alertas, devuelve [].
""" + REGLAS_COMUNES

PROMPTS = {"bases": PROMPT, "absolucion": PROMPT_ABSOLUCION, "generico": PROMPT_GENERICO}
SEACE = "https://prod1.seace.gob.pe/SeaceWeb-PRO/SdescargarArchivoAlfresco?fileCode="
DOC_BASES = {"k": "bases", "nombre": "Bases", "url": "", "file_code": ""}   # lo que se analiza cuando nadie eligió otro documento
PREFIERE = {"absolucion": ("absolu", "pliego", "consulta")}                 # palabras que orientan qué archivo de un ZIP/RAR usar


# --------------------------------------------------------------------------- utilidades
def ahora():
    return datetime.now(timezone.utc)


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def tipo_bytes(data):
    for firma, nombre in ((b"%PDF", "PDF"), (b"PK\x03\x04", "ZIP"), (b"Rar!", "RAR"), (b"7z\xbc\xaf\x27\x1c", "7Z")):
        if data[:len(firma)] == firma:
            return nombre
    return None


MAX_DOCS = 3                   # documentos por proceso que se mandan a Gemini
MAX_TOTAL_MB = 45              # tope de tamaño total de lo que se sube


def elegir_docs(nombres_tamanos, maximo=MAX_DOCS, prefiere=()):
    """De [(nombre, tamaño)] elige hasta `maximo` PDF/Word. Primero los que contengan alguna palabra de `prefiere`
    (para documentos que no son bases); luego 'integrad', 'base', TDR y, al final, los más grandes."""
    ok = [(n, t) for n, t in nombres_tamanos
          if n.lower().endswith((".pdf", ".docx")) and not n.endswith("/") and "__macosx" not in n.lower()]
    def clave(x):
        n = x[0].lower()
        if prefiere and any(p in norm(n) for p in prefiere):
            return (-1, -x[1])
        return (0 if "integrad" in n else 1 if "base" in n else 2 if ("tdr" in n or "termino" in n or "término" in n) else 3, -x[1])
    elegidos, total = [], 0
    for n, t in sorted(ok, key=clave):
        if len(elegidos) >= maximo or (elegidos and total + t > MAX_TOTAL_MB * 1e6):
            break
        elegidos.append(n)
        total += t
    return elegidos


def es_docx(z):
    nombres = z.namelist()
    return "word/document.xml" in nombres


def texto_docx(z):
    """Texto de un .docx (ya abierto como ZipFile): un párrafo por línea, celdas de tabla separadas por tabulación."""
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    raiz = ET.fromstring(z.read("word/document.xml"))
    lineas = []
    for p in raiz.iter(ns + "p"):
        partes = []
        for e in p.iter():
            if e.tag == ns + "t" and e.text:
                partes.append(e.text)
            elif e.tag in (ns + "tab", ns + "br"):
                partes.append("\t" if e.tag == ns + "tab" else "\n")
        t = "".join(partes).strip()
        if t:
            lineas.append(t)
    return "\n".join(lineas)


# --------------------------------------------------------------------------- bases
def url_de_bases(client, nom):
    """'Bases Integradas' o 'Bases Administrativas' de documentos_proceso; si no hay, url_bases del proceso."""
    docs = client.table("documentos_proceso").select("categoria,documento,nombre_archivo,url_descarga") \
        .eq("nomenclatura_norm", nom).execute().data or []
    def prioridad(d):
        t = " ".join(str(d.get(k) or "") for k in ("categoria", "documento", "nombre_archivo")).lower()
        return 0 if "integrad" in t else 1 if "administrativ" in t else 2
    docs = [d for d in docs if d.get("url_descarga")]
    cand = sorted(docs, key=prioridad)
    if cand and prioridad(cand[0]) < 2:
        return cand[0]["url_descarga"]
    for vista in ("alertas_filtradas", "oportunidades_abiertas"):
        r = client.table(vista).select("url_bases").eq("nomenclatura_norm", nom).limit(1).execute().data or []
        if r and r[0].get("url_bases"):
            return r[0]["url_bases"]
    return cand[0]["url_descarga"] if cand else None


# --------------------------------------------------------------------------- tipo y enlace del documento
def norm(s):
    return unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()


def tipo_doc(nombre):
    """'bases', 'absolucion' o 'generico', según el nombre del documento."""
    t = norm(nombre)
    if "absolu" in t or "pliego" in t:
        return "absolucion"
    if "integrad" in t or "base" in t or "administrativ" in t:
        return "bases"
    return "generico"


def doc_de_seguimiento(s):
    """Documento pedido en analisis_seguimiento.ia_documento ({k, nombre, url, file_code}); sin él, las bases."""
    x = s.get("ia_documento")
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            x = None
    if not isinstance(x, dict):
        return dict(DOC_BASES)
    return {"k": str(x.get("k") or x.get("url") or x.get("nombre") or "bases"), "nombre": x.get("nombre") or "Bases",
            "url": x.get("url") or "", "file_code": x.get("file_code") or ""}


def doc_por_texto(client, nom, texto):
    """Para --doc: el documento publicado del proceso cuyo nombre contenga `texto` (sin tildes ni mayúsculas)."""
    docs = client.table("documentos_proceso").select("file_id,categoria,documento,nombre_archivo,file_code,url_descarga") \
        .eq("nomenclatura_norm", nom).execute().data or []
    for d in docs:
        if norm(texto) in norm(" ".join(str(d.get(k) or "") for k in ("categoria", "documento", "nombre_archivo"))):
            url = d.get("url_descarga") or (SEACE + d["file_code"] if d.get("file_code") else "")
            return {"k": str(d.get("file_id") or d.get("file_code") or url), "nombre": d.get("nombre_archivo") or d.get("documento") or d.get("categoria") or texto,
                    "url": url, "file_code": d.get("file_code") or ""}
    return None


def url_de_doc(client, nom, doc, tipo):
    """Enlace del documento pedido; para las bases sin documento elegido, el de siempre."""
    if doc.get("url"):
        return doc["url"]
    if doc.get("file_code"):
        return SEACE + doc["file_code"]
    return url_de_bases(client, nom) if tipo == "bases" else None


def bajar(url):
    r = requests.get(url, headers=UA, timeout=120, stream=True, allow_redirects=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for trozo in r.iter_content(1 << 20):
        buf.write(trozo)
        if buf.tell() > MAX_MB * 1e6:
            raise RuntimeError(f"El archivo supera {MAX_MB} MB; no se analiza automáticamente.")
    return buf.getvalue()


def _guardar(nombre, contenido, carpeta, i):
    """Guarda un PDF o un Word (convertido a texto). Devuelve (ruta, mime) o None si el Word no tiene texto."""
    base = Path(carpeta)
    if nombre.lower().endswith(".docx"):
        z = zipfile.ZipFile(io.BytesIO(contenido))
        txt = texto_docx(z) if es_docx(z) else ""
        if not txt.strip():
            return None
        ruta = base / f"doc{i}.txt"
        ruta.write_text(txt, encoding="utf-8")
        return ruta, "text/plain"
    ruta = base / f"doc{i}.pdf"
    ruta.write_bytes(contenido)
    return ruta, "application/pdf"


def obtener_documentos(data, carpeta, tipo_doc_="bases"):
    """Devuelve ([(ruta, mime), ...], descripcion). Abre ZIP/RAR; si es un Word, lo convierte a texto."""
    tipo = tipo_bytes(data)
    if tipo == "PDF":
        r = _guardar("bases.pdf", data, carpeta, 1)
        return [r], f"PDF directo ({len(data) / 1e6:.1f} MB)"
    if tipo == "ZIP":
        z = zipfile.ZipFile(io.BytesIO(data))
        if es_docx(z):
            r = _guardar("bases.docx", data, carpeta, 1)
            if not r:
                raise RuntimeError("El documento Word no tiene texto legible.")
            return [r], "Word (.docx) convertido a texto"
        info = [(i.filename, i.file_size) for i in z.infolist()]
        abrir = z.read
    elif tipo == "RAR":
        try:
            import rarfile
        except ImportError:
            raise RuntimeError("Falta la librería rarfile: pip install rarfile")
        try:
            rf = rarfile.RarFile(io.BytesIO(data))
            info = [(i.filename, i.file_size) for i in rf.infolist()]
        except rarfile.RarCannotExec:
            raise RuntimeError("No se pudo abrir el RAR: instala una herramienta con  sudo apt install unar")
        abrir = rf.read
    else:
        raise RuntimeError(f"Formato no soportado ({tipo or 'desconocido'}); abre las bases manualmente.")
    print("  contenido: " + ", ".join(n for n, _ in info[:12]) + (" …" if len(info) > 12 else ""))
    elegidos = elegir_docs(info, MAX_DOCS if tipo_doc_ == "bases" else 1, PREFIERE.get(tipo_doc_, ()))
    if not elegidos:
        raise RuntimeError("El archivo comprimido no contiene PDF ni Word.")
    docs = []
    for i, n in enumerate(elegidos, 1):
        r = _guardar(n, abrir(n), carpeta, i)
        if r:
            docs.append(r)
    if not docs:
        raise RuntimeError("Los documentos del archivo no tienen texto legible.")
    return docs, f"{tipo} → " + " + ".join(elegidos)


# --------------------------------------------------------------------------- Gemini
def analizar_con_gemini(docs, modelo=None, tipo="bases"):
    from google import genai
    from google.genai import types
    cli = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    subidos = []
    try:
        for ruta, mime in docs:
            subidos.append(cli.files.upload(file=str(ruta), config=types.UploadFileConfig(mime_type=mime)))
        for k, f in enumerate(subidos):
            for _ in range(90):
                if getattr(f.state, "name", str(f.state)) != "PROCESSING":
                    break
                time.sleep(2)
                f = cli.files.get(name=f.name)
            subidos[k] = f
            if getattr(f.state, "name", str(f.state)) == "FAILED":
                raise RuntimeError("Gemini no pudo procesar uno de los documentos.")
        resp, usado, ultimo = None, None, None
        cadena = modelos_a_probar(cli, modelo)
        print("  modelos a probar: " + ", ".join(cadena))
        for m in cadena:
            for intento in range(3):       # un 503 suele ser pasajero: reintenta el mismo modelo antes de cambiar
                try:
                    resp = cli.models.generate_content(
                        model=m, contents=subidos + [prompt_actual(tipo)],
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
                    usado = m
                    break
                except Exception as e:
                    ultimo = e
                    if re.search(r"\b503\b|UNAVAILABLE", str(e)) and intento < 2:
                        espera = (8, 20)[intento]
                        print(f"  {m} saturado (503); reintento en {espera} s")
                        time.sleep(espera)
                        continue
                    if re.search(r"\b(404|429|503)\b|NOT_FOUND|UNAVAILABLE|RESOURCE_EXHAUSTED", str(e)):
                        print(f"  modelo {m} no disponible o sin cuota ({str(e)[:60]}…); pruebo el siguiente")
                        break
                    raise
            if resp is not None:
                break
        if resp is None:
            raise ultimo
        texto = (resp.text or "").strip()
    finally:
        for f in subidos:
            try:
                cli.files.delete(name=f.name)
            except Exception:
                pass
    texto = re.sub(r"^```(?:json)?|```$", "", texto, flags=re.M).strip()
    try:
        return json.loads(texto), usado
    except json.JSONDecodeError:
        raise RuntimeError("Gemini no devolvió un JSON válido: " + texto[:200])


def _txt(v):
    if isinstance(v, list):
        return "\n".join("• " + (json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)) for x in v)
    return "" if v is None else str(v)


def prompt_actual(tipo="bases"):
    """El prompt del tipo de documento con la fecha de hoy (hora de Perú), para que la IA pueda decir si un plazo ya venció."""
    hoy = datetime.now(timezone(timedelta(hours=-5))).strftime("%d/%m/%Y")
    return PROMPTS.get(tipo, PROMPT_GENERICO).replace("{HOY}", hoy)


def _monto(v):
    """Primer monto de un texto ('S/ 319,973.78 (incl. IGV)') como Decimal, o None."""
    m = re.search(r"\d[\d.,]*", str(v or ""))
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


CITA_PY = re.compile(r"\((?:Cap\.|Cap[ií]tulo|Secci[oó]n|Anexo|P[aá]gs?\.|Num\.|Numeral)[^()]*\)", re.I)


def _cita(v):
    """Primera cita de las bases, ej. '(Sección I, numeral 1.4, pág. 5)', dentro de un texto; '' si no hay."""
    m = CITA_PY.search(str(v or ""))
    return m.group(0) if m else ""


def ajustar_limites(d):
    """Si las bases traen la nota de redondeo (inferior hacia arriba, superior sin redondear), calcula los límites a partir
    del valor referencial con esa regla. Solo toca un límite si ya era ~90 % o ~110 % del valor referencial."""
    regla = str(d.get("regla_redondeo_limites") or "")
    vr = _monto(d.get("valor_referencial"))
    if vr is None or not regla or re.match(r"\s*no consta", regla, re.I):
        return
    sube = re.search(r"aument\w*\s+en\s+un\s+d[ií]gito|hacia\s+arriba|por\s+exceso|al\s+alza", regla, re.I)
    trunca = re.search(r"sin\s+(?:efectuar\s+)?(?:el\s+)?redondeo|trunc", regla, re.I)
    for clave, factor, modo, pct, aplica in (("limite_inferior", Decimal("0.90"), ROUND_CEILING, "90 %", sube),
                                             ("limite_superior", Decimal("1.10"), ROUND_FLOOR, "110 %", trunca)):
        actual, crudo = _monto(d.get(clave)), vr * factor
        if not aplica or actual is None or abs(actual - crudo) > Decimal("0.05"):
            continue
        cita = _cita(d.get(clave)) or _cita(d.get("valor_referencial")) or _cita(regla)   # no perder la página al reescribir el monto
        d[clave] = f"S/ {crudo.quantize(Decimal('0.01'), rounding=modo):,.2f} ({pct} del valor referencial)" + (f" {cita}" if cita else "")


def modelos_a_probar(cli, forzado=None):
    """Modelos Gemini a intentar, en orden: el forzado en el .env (si hay), los 3 Flash más nuevos que la cuenta ve hoy y,
    de último recurso, el Lite más nuevo. Si no se puede consultar la lista, usa los nombres fijos de arriba."""
    def version(nombre):
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-flash(-lite)?", nombre)
        return (tuple(int(x) for x in m.group(1).split(".")), bool(m.group(2))) if m else None
    nombres = []
    try:
        for m in cli.models.list():
            acciones = getattr(m, "supported_actions", None)
            if acciones is None or "generateContent" in acciones:
                nombres.append(str(m.name).split("/")[-1])
    except Exception as e:
        print(f"  (no pude consultar los modelos de Gemini: {str(e)[:80]}; uso la lista fija)")
    conocidos = [(version(n), n) for n in nombres if version(n)]
    flash = [n for _, n in sorted((x for x in conocidos if not x[0][1]), reverse=True)]
    lite = [n for _, n in sorted((x for x in conocidos if x[0][1]), reverse=True)]
    cadena = ([forzado] if forzado else []) + flash[:3] + lite[:1]
    if not (flash or lite):
        cadena += [MODELO_DEFECTO] + MODELOS_RESPALDO
    vistos = set()
    return [m for m in cadena if m and not (m in vistos or vistos.add(m))]


# --------------------------------------------------------------------------- estado en la BD
def marcar(client, nom, doc, **campos):
    """Guarda/actualiza la fila de ese documento del proceso (clave: nomenclatura_norm + doc_key)."""
    campos.update(nomenclatura_norm=nom, doc_key=doc["k"], doc_nombre=doc["nombre"], doc_url=doc.get("url") or None,
                  updated_at=ahora().isoformat())
    client.table("analisis_ia").upsert(campos, on_conflict="nomenclatura_norm,doc_key").execute()


def pendientes(client):
    """[(nomenclatura, documento)] por atender: lo pedido en analisis_seguimiento que aún no tiene resumen o se volvió a pedir."""
    seg = client.table("analisis_seguimiento").select("nomenclatura_norm,ia_pedido_at,ia_documento") \
        .not_.is_("ia_pedido_at", "null").execute().data or []
    if not seg:
        return []
    filas = client.table("analisis_ia").select("nomenclatura_norm,doc_key,estado,analizado_at,updated_at") \
        .in_("nomenclatura_norm", [s["nomenclatura_norm"] for s in seg]).execute().data or []
    ia = {(r["nomenclatura_norm"], r.get("doc_key") or "bases"): r for r in filas}
    out = []
    for s in sorted(seg, key=lambda x: x["ia_pedido_at"]):
        doc = doc_de_seguimiento(s)
        r = ia.get((s["nomenclatura_norm"], doc["k"]))
        if r is None:
            out.append((s["nomenclatura_norm"], doc))
        elif r.get("estado") == "listo":
            # ya analizado: solo se repite si volvieron a pedirlo después (botón «Volver a resumir»)
            a, p = _dt(r.get("analizado_at")), _dt(s["ia_pedido_at"])
            if a and p and p > a:
                out.append((s["nomenclatura_norm"], doc))
        elif r.get("estado") == "procesando":
            u = _dt(r.get("updated_at"))
            if u is None or ahora() - u > timedelta(minutes=COLGADO_MIN):
                out.append((s["nomenclatura_norm"], doc))
        else:  # error o pendiente: solo si volvieron a pedirlo después del último intento
            a, p = _dt(r.get("analizado_at")), _dt(s["ia_pedido_at"])
            if a is None or (p and p > a):
                out.append((s["nomenclatura_norm"], doc))
    return out


def pedido_de(client, nom):
    """ia_pedido_at actual del proceso (None si se canceló la solicitud o ya no está en la lista)."""
    r = client.table("analisis_seguimiento").select("ia_pedido_at").eq("nomenclatura_norm", nom).limit(1).execute().data or []
    return r[0].get("ia_pedido_at") if r else None


def deshacer_procesando(client, nom, doc):
    """Solicitud cancelada (o reemplazada) mientras se procesaba: no se guarda nada y la fila vuelve a como estaba
    (a «listo» si ya tenía un resumen anterior; si no, se borra)."""
    q = client.table("analisis_ia").select("id,analizado_at,detalle").eq("nomenclatura_norm", nom).eq("doc_key", doc["k"]).limit(1).execute().data or []
    if not q:
        return
    if q[0].get("analizado_at") and q[0].get("detalle"):
        client.table("analisis_ia").update({"estado": "listo", "error": None, "updated_at": ahora().isoformat()}).eq("id", q[0]["id"]).execute()
    else:
        client.table("analisis_ia").delete().eq("id", q[0]["id"]).execute()


def procesar(client, nom, doc, modelo, dry_run=False, de_cola=False):
    tipo = tipo_doc(doc["nombre"])
    print(f"\n{nom} · {doc['nombre']} [{tipo}]")
    ped0 = None if dry_run else pedido_de(client, nom)
    if de_cola and not dry_run and ped0 is None:
        print("  la solicitud se canceló antes de empezar; se omite.")
        return True
    vigilar = ped0 is not None          # solo las solicitudes de la web se pueden cancelar (no --una a mano)
    try:
        url = url_de_doc(client, nom, doc, tipo)
        if not url:
            raise RuntimeError("El proceso no tiene enlace de bases." if tipo == "bases" else "El documento no tiene enlace de descarga.")
        doc = dict(doc, url=url)
        if not dry_run:
            marcar(client, nom, doc, estado="procesando", error=None)
        data = bajar(url)
        with tempfile.TemporaryDirectory() as tmp:
            docs, desc = obtener_documentos(data, tmp, tipo)
            print(f"  documento: {desc}")
            if dry_run:
                print("  (dry-run) no se llamó a Gemini ni se guardó nada.")
                return True
            d, usado = analizar_con_gemini(docs, modelo, tipo)
            if vigilar and pedido_de(client, nom) != ped0:     # cancelada (o pedida otra vez) mientras Gemini trabajaba
                deshacer_procesando(client, nom, doc)
                print("  la solicitud se canceló mientras se procesaba; no se guardó el resultado.")
                return True
            if tipo == "bases":
                ajustar_limites(d)
        if tipo == "bases":   # estas columnas resumen solo tienen sentido para las bases
            extra = dict(resumen=_txt(d.get("objeto_alcance")), plazo=_txt(d.get("plazo")), experiencia_requerida=_txt(d.get("experiencia_postor")))
        else:
            extra = dict(resumen=_txt(d.get("resumen")))
        marcar(client, nom, doc, estado="listo", error=None, modelo=usado, analizado_at=ahora().isoformat(), detalle=d, **extra)
        print(f"  listo: análisis guardado (modelo {usado}).")
        return True
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:500]
        print(f"  ERROR: {msg}", file=sys.stderr)
        if not dry_run:
            try:
                marcar(client, nom, doc, estado="error", error=msg, analizado_at=ahora().isoformat())
            except Exception as e2:
                print(f"  (tampoco pude guardar el error: {e2})", file=sys.stderr)
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--una", metavar="NOMENCLATURA", help="analiza solo este proceso (sin esperar la solicitud de la web)")
    ap.add_argument("--doc", metavar="TEXTO", help="con --una: analiza el documento del proceso cuyo nombre contenga este texto (ej. absol); sin esto, las bases")
    ap.add_argument("--dry-run", action="store_true", help="descarga el documento y muestra qué archivo usaría; no llama a Gemini ni guarda")
    ap.add_argument("--cada", type=int, metavar="SEG", help="queda corriendo y revisa solicitudes cada SEG segundos")
    a = ap.parse_args(argv)

    load_env()
    if not a.dry_run and not os.environ.get("GEMINI_API_KEY"):
        print("[!] Falta GEMINI_API_KEY en el .env", file=sys.stderr)
        return 1
    modelo = os.environ.get("GEMINI_MODEL") or None
    client = get_client()

    if a.una:
        doc = dict(DOC_BASES)
        if a.doc:
            doc = doc_por_texto(client, a.una, a.doc)
            if not doc:
                print(f"[!] {a.una} no tiene ningún documento publicado cuyo nombre contenga «{a.doc}».", file=sys.stderr)
                return 2
        return 0 if procesar(client, a.una, doc, modelo, a.dry_run) else 2
    while True:
        cola = pendientes(client)
        print(f"{ahora().astimezone().strftime('%H:%M:%S')} · pendientes: {len(cola)}")
        for i, (nom, doc) in enumerate(cola):
            if i:
                time.sleep(PAUSA_ENTRE_PROCESOS)
            procesar(client, nom, doc, modelo, a.dry_run, de_cola=True)
        if not a.cada:
            return 0
        time.sleep(a.cada)


if __name__ == "__main__":
    sys.exit(main())