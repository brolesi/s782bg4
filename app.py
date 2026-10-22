"""CRM de propostas: clientes, propostas, versões, anexos, follow-ups, histórico de status. Rode: python app.py"""
import os
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, g, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

BASE = Path(__file__).parent
DB = Path(os.environ.get("CRM_DB", BASE / "crm.db"))
UPLOADS = Path(os.environ.get("CRM_UPLOADS", BASE / "uploads"))
STATUS = ["Rascunho", "Enviada", "Em negociação", "Ganha", "Perdida", "Esfriou"]
ABERTOS = ("Rascunho", "Enviada", "Em negociação")
BOLA = ["Comigo", "Com o cliente"]
MOTIVOS = ["Preço", "Fechou com concorrente", "Escopo", "Sem orçamento", "Sem resposta", "Outro"]
# colunas adicionadas depois da 1ª versão; init_db cria nas bases antigas
NOVAS_COLUNAS = {"proximo_passo": "TEXT", "bola_com": "TEXT", "data_decisao": "TEXT", "motivo_perda": "TEXT"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
  id INTEGER PRIMARY KEY, nome TEXT NOT NULL, contato TEXT, email TEXT, telefone TEXT, notas TEXT,
  criado_em TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS propostas (
  id INTEGER PRIMARY KEY, cliente_id INTEGER NOT NULL REFERENCES clientes(id), titulo TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Rascunho', proximo_contato TEXT, notas TEXT,
  criado_em TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS versoes (
  id INTEGER PRIMARY KEY, proposta_id INTEGER NOT NULL REFERENCES propostas(id), numero INTEGER NOT NULL,
  descricao TEXT, valor REAL, link_precificacao TEXT, criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (proposta_id, numero));
-- versao_id preenchido = anexo da versão; vazio = arquivo adicional da proposta
CREATE TABLE IF NOT EXISTS arquivos (
  id INTEGER PRIMARY KEY, proposta_id INTEGER NOT NULL REFERENCES propostas(id), versao_id INTEGER REFERENCES versoes(id),
  nome TEXT NOT NULL, caminho TEXT NOT NULL, criado_em TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY, proposta_id INTEGER NOT NULL REFERENCES propostas(id), data TEXT NOT NULL,
  canal TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS status_historico (
  id INTEGER PRIMARY KEY, proposta_id INTEGER NOT NULL REFERENCES propostas(id), status TEXT NOT NULL,
  motivo TEXT, data TEXT DEFAULT CURRENT_TIMESTAMP);
-- valor da proposta = valor da última versão
CREATE VIEW IF NOT EXISTS propostas_v AS
  SELECT p.*, c.nome AS cliente,
    (SELECT valor FROM versoes v WHERE v.proposta_id = p.id ORDER BY numero DESC LIMIT 1) AS valor,
    (SELECT MAX(numero) FROM versoes v WHERE v.proposta_id = p.id) AS ultima_versao
  FROM propostas p JOIN clientes c ON c.id = p.cliente_id;
"""

app = Flask(__name__)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def fechar(_):
    if (con := g.pop("db", None)) is not None:
        con.close()


def init_db():
    UPLOADS.mkdir(exist_ok=True)
    with sqlite3.connect(DB) as con:
        con.executescript(SCHEMA)
        existentes = {r[1] for r in con.execute("PRAGMA table_info(propostas)")}
        for col, tipo in NOVAS_COLUNAS.items():
            if col not in existentes:
                con.execute(f"ALTER TABLE propostas ADD COLUMN {col} {tipo}")


def form(*campos):
    return [request.form.get(c, "").strip() or None for c in campos]


def valor(txt):
    """Aceita '1.234,56' ou '1234.56'."""
    if not txt:
        return None
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        abort(400, f"Valor inválido: {txt}")


def link(txt):
    if txt and not txt.lower().startswith(("http://", "https://")):
        abort(400, "Link da planilha deve começar com http:// ou https://")
    return txt


def salvar_arquivo(proposta_id, versao_id=None):
    f = request.files.get("arquivo")
    if not f or not f.filename:
        return
    caminho = f"{uuid.uuid4().hex}_{secure_filename(f.filename) or 'arquivo'}"
    f.save(UPLOADS / caminho)
    db().execute("INSERT INTO arquivos (proposta_id, versao_id, nome, caminho) VALUES (?,?,?,?)",
                 (proposta_id, versao_id, f.filename, caminho))


@app.template_filter("brl")
def brl(v):
    return "—" if v is None else f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.context_processor
def globais():
    return {"STATUS": STATUS, "ABERTOS": ABERTOS, "BOLA": BOLA, "MOTIVOS": MOTIVOS, "hoje": date.today().isoformat()}


# ---------- Esta semana ----------
@app.get("/")
def semana():
    hoje = date.today()
    fim = (hoje + timedelta(days=7)).isoformat()
    abertas = db().execute(
        f"SELECT * FROM propostas_v WHERE status IN ({','.join('?' * len(ABERTOS))}) "
        "AND (proximo_contato IS NULL OR proximo_contato <= ?) ORDER BY proximo_contato", (*ABERTOS, fim)).fetchall()
    h = hoje.isoformat()
    grupos = {
        "Atrasados": [p for p in abertas if p["proximo_contato"] and p["proximo_contato"] < h],
        "Hoje": [p for p in abertas if p["proximo_contato"] == h],
        "Próximos 7 dias": [p for p in abertas if p["proximo_contato"] and p["proximo_contato"] > h],
        "Sem próximo contato marcado": [p for p in abertas if not p["proximo_contato"]],
    }
    return render_template("semana.html", grupos=grupos)


# ---------- Pipeline ----------
@app.get("/pipeline")
def pipeline():
    status = request.args.get("status")
    sql = "SELECT * FROM propostas_v"
    args = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY proximo_contato IS NULL, proximo_contato, criado_em DESC"
    propostas = db().execute(sql, args).fetchall()
    resumo = db().execute(
        "SELECT status, COUNT(*) n, SUM(valor) total FROM propostas_v GROUP BY status").fetchall()
    return render_template("pipeline.html", propostas=propostas, resumo=resumo, filtro=status)


# ---------- Clientes ----------
@app.get("/clientes")
def clientes():
    rows = db().execute("""SELECT c.*, COUNT(p.id) n FROM clientes c LEFT JOIN propostas p ON p.cliente_id = c.id
                           GROUP BY c.id ORDER BY c.nome""").fetchall()
    return render_template("clientes.html", clientes=rows)


@app.route("/clientes/novo", methods=["GET", "POST"])
@app.route("/clientes/<int:id>", methods=["GET", "POST"])
def cliente(id=None):
    if request.method == "POST":
        dados = form("nome", "contato", "email", "telefone", "notas")
        if not dados[0]:
            abort(400, "Nome é obrigatório")
        if id:
            db().execute("UPDATE clientes SET nome=?, contato=?, email=?, telefone=?, notas=? WHERE id=?", (*dados, id))
        else:
            id = db().execute("INSERT INTO clientes (nome, contato, email, telefone, notas) VALUES (?,?,?,?,?)",
                              dados).lastrowid
        db().commit()
        return redirect(url_for("cliente", id=id))
    c = db().execute("SELECT * FROM clientes WHERE id=?", (id,)).fetchone() if id else None
    if id and not c:
        abort(404)
    propostas = db().execute("SELECT * FROM propostas_v WHERE cliente_id=? ORDER BY criado_em DESC", (id,)).fetchall()
    return render_template("cliente.html", c=c, propostas=propostas)


# ---------- Propostas ----------
@app.route("/propostas/nova", methods=["GET", "POST"])
@app.route("/propostas/<int:id>", methods=["GET", "POST"])
def proposta(id=None):
    if request.method == "POST":
        campos = ["cliente_id", "titulo", "status", "proximo_contato", "notas",
                  "proximo_passo", "bola_com", "data_decisao", "motivo_perda"]
        d = dict(zip(campos, form(*campos)))
        if not (d["cliente_id"] and d["titulo"]) or d["status"] not in STATUS:
            abort(400, "Cliente, título e status são obrigatórios")
        if d["bola_com"] not in (None, *BOLA):
            abort(400, "Valor inválido para 'bola com'")
        if d["status"] == "Perdida":
            if d["motivo_perda"] not in MOTIVOS:
                abort(400, "Informe o motivo da perda")
        else:
            d["motivo_perda"] = None
        con = db()
        if id:
            antes = con.execute("SELECT status, motivo_perda FROM propostas WHERE id=?", (id,)).fetchone() or abort(404)
            con.execute(f"UPDATE propostas SET {', '.join(c + '=?' for c in campos)} WHERE id=?", (*d.values(), id))
            mudou = (antes["status"], antes["motivo_perda"]) != (d["status"], d["motivo_perda"])
        else:
            id = con.execute(f"INSERT INTO propostas ({', '.join(campos)}) VALUES ({','.join('?' * len(campos))})",
                             tuple(d.values())).lastrowid
            mudou = True
        if mudou:
            con.execute("INSERT INTO status_historico (proposta_id, status, motivo) VALUES (?,?,?)",
                        (id, d["status"], d["motivo_perda"]))
        con.commit()
        return redirect(url_for("proposta", id=id))
    p = db().execute("SELECT * FROM propostas_v WHERE id=?", (id,)).fetchone() if id else None
    if id and not p:
        abort(404)
    q = lambda sql: db().execute(sql, (id,)).fetchall()
    return render_template(
        "proposta.html", p=p,
        clientes=db().execute("SELECT id, nome FROM clientes ORDER BY nome").fetchall(),
        cliente_id=request.args.get("cliente_id", type=int),
        versoes=q("SELECT * FROM versoes WHERE proposta_id=? ORDER BY numero DESC"),
        arquivos=q("SELECT * FROM arquivos WHERE proposta_id=? ORDER BY criado_em DESC"),
        followups=q("SELECT * FROM followups WHERE proposta_id=? ORDER BY data DESC, id DESC"),
        historico=q("SELECT * FROM status_historico WHERE proposta_id=? ORDER BY id DESC"))


@app.post("/propostas/<int:id>/versoes")
def nova_versao(id):
    descricao, v, lk = form("descricao", "valor", "link_precificacao")
    con = db()
    numero = con.execute("SELECT COALESCE(MAX(numero), 0) + 1 FROM versoes WHERE proposta_id=?", (id,)).fetchone()[0]
    vid = con.execute("INSERT INTO versoes (proposta_id, numero, descricao, valor, link_precificacao) VALUES (?,?,?,?,?)",
                      (id, numero, descricao, valor(v), link(lk))).lastrowid
    salvar_arquivo(id, vid)
    con.commit()
    return redirect(url_for("proposta", id=id))


@app.post("/versoes/<int:vid>")
def editar_versao(vid):
    descricao, v, lk = form("descricao", "valor", "link_precificacao")
    row = db().execute("SELECT proposta_id FROM versoes WHERE id=?", (vid,)).fetchone() or abort(404)
    db().execute("UPDATE versoes SET descricao=?, valor=?, link_precificacao=? WHERE id=?",
                 (descricao, valor(v), link(lk), vid))
    salvar_arquivo(row["proposta_id"], vid)
    db().commit()
    return redirect(url_for("proposta", id=row["proposta_id"]))


@app.post("/propostas/<int:id>/arquivos")
def novo_arquivo(id):
    salvar_arquivo(id)
    db().commit()
    return redirect(url_for("proposta", id=id))


@app.post("/propostas/<int:id>/followups")
def novo_followup(id):
    data, canal, nota, proximo = form("data", "canal", "nota", "proximo_contato")
    con = db()
    con.execute("INSERT INTO followups (proposta_id, data, canal, nota) VALUES (?,?,?,?)",
                (id, data or date.today().isoformat(), canal, nota))
    if proximo:
        con.execute("UPDATE propostas SET proximo_contato=? WHERE id=?", (proximo, id))
    con.commit()
    return redirect(url_for("proposta", id=id))


@app.get("/arquivos/<int:aid>")
def baixar(aid):
    a = db().execute("SELECT * FROM arquivos WHERE id=?", (aid,)).fetchone() or abort(404)
    return send_from_directory(UPLOADS, a["caminho"], as_attachment=True, download_name=a["nome"])


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
