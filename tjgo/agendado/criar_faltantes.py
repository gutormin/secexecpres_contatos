"""Cria e atualiza endereçamentos de titulares da lista do TJGO.

Roda todo dia pelo RODAR.bat, logo depois do tjgo_sync.py. Fica em Downloads\\TJGO.
1. Lê a lista do TJGO (documento tjgo/lista do Firebase; se não der, tjgo_tabela.json).
2. Lê todos os endereçamentos (coleção "enderecos").
3. Para cada titular que não aparece em nenhum endereçamento, cria um "Ofício simples"
   já vinculado à Comarca/Vara, no mesmo formato do formulário do sistema.
4. Para cada titular com endereçamento existente, verifica se a comarca mudou; se sim,
   atualiza os campos cargo e localidade.
5. Escreve o que fez em criar_faltantes.log (nesta pasta).

Conexão com o Firebase: usa as mesmas credenciais do tjgo_sync.py (Documentos\\TJGO).
Aceita conta de serviço (.json, via firebase_admin) ou e-mail e senha do sistema.
Opções: --simular (só mostra, não grava)   --forcar (ignora o limite de segurança)
"""
import datetime as dt
import glob
import json
import os
import random
import re
import string
import sys
import traceback
import unicodedata
import urllib.error
import urllib.request

PASTA = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(PASTA, "criar_faltantes.log")
TABELA = os.path.join(PASTA, "tjgo_tabela.json")
PROJETO = "secexecpres-contatos"
API_KEY_SITE = "AIzaSyAb41PleAsglzBLxQ6iFW3r2-cl15bYMCk"  # chave pública do site
LIMITE = 120  # acima disso, algo mudou na lista: não cria nada sem --forcar
SIMULAR = "--simular" in sys.argv
FORCAR = "--forcar" in sys.argv
ROBO = "Robô TJGO (criar_faltantes.py)"

CASA = os.path.expanduser("~")
PASTAS_CRED = [PASTA] + [os.path.join(CASA, *p, "TJGO") for p in
                         (("Documents",), ("Documentos",), ("OneDrive", "Documents"),
                          ("OneDrive", "Documentos"))]


def log(msg):
    linha = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linha)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


# ---------------------------------------------------------------- texto
def norm(v):
    """Igual ao verificador do site: sem acento, maiúsculas, só letras e espaços."""
    s = unicodedata.normalize("NFD", str(v or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").upper()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z ]", " ", s)).strip()


def titular_limpo(pessoa):
    s = re.split(r"\s+(?:Resp\.|DJ\b|\(| - )", str(pessoa or ""), flags=re.I)[0].strip()
    return re.sub(r"(\s+\S)+$", "", s)  # tira letra solta no fim (nome cortado no site)


def comarca_curta(comarca):
    s = re.sub(r"\s*(\(|\bD\.?J\b|\bLei\b).*$", "", str(comarca or ""), flags=re.I).strip()
    return s or str(comarca or "").strip()


def titulo(s):
    miudas = {"de", "da", "do", "das", "dos", "e"}
    pal = s.lower().split()
    return " ".join(p if (i and p in miudas) else p[:1].upper() + p[1:] for i, p in enumerate(pal))


def vara_limpa(vara):
    s = str(vara or "").split("(")[0]
    s = re.sub(r"\s+(Criad[ao]|Instalad[ao]|Altera[çc][ãa]o|Alterad[ao]|Res\.|Resolu[çc][ãa]o|Lei|DJ|D\.J)\b.*$",
               "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" .,;-–")


def artigo(vara):
    w = norm(vara).split()
    if not w:
        return ""
    if re.match(r"^\d+\s*ª", vara):
        return "da"
    if re.match(r"^\d+\s*[º°o]", vara):
        return "do"
    p = w[0]
    if p in ("VARA", "TURMA", "CAMARA", "CENTRAL", "AUDITORIA", "SECRETARIA", "UNIDADE", "DIRETORIA"):
        return "da"
    if p in ("JUIZADO", "NUCLEO", "JUIZO", "TRIBUNAL", "CEJUSC", "PLANTAO", "FORO", "CENTRO"):
        return "do"
    return "da" if p.endswith("A") else "do"


# ---------------------------------------------------------------- gênero
FEM = set("""BEATRIZ RAQUEL RACHEL ISABEL ISABELE ISABELLE LAIS THAIS TAIS INES IRIS LIZ MIRIAM MIRIAN
RUTH RUTE ESTER ESTHER CARMEN CARMEM KAREN KARIN ELLEN ELEN SUELEN SUELLEN LILIAN VIVIAN YASMIN JASMIN
NOEMI NAIR DORIS KELLY SHIRLEY MARY NATHALIE SOLANGE MABEL LUZ MARILIA ROSIMEIRE ROSEMEIRE
MARLI SUELI NELI ROSELI JUCELI ARACI IRACI ZELI DALVANI LEIDIANE""".split())
MASC = set("""JOSHUA LUCA NIKOLA JONATA JONATHA ZACARIA JOSUA""".split())
FEM_FIM = ("INE", "ANE", "ENE", "ICE", "ISE", "ELE", "ELLE", "EIDE", "EIRE", "ETE", "ETTE", "ILDE")


def primeiro_nome(nome):
    w = [p for p in norm(nome).split() if p not in ("DESEMBARGADOR", "DESEMBARGADORA", "JUIZ",
                                                     "JUIZA", "DR", "DRA", "DOUTOR", "DOUTORA")]
    return w[0] if w else ""


def aprender_generos(enderecos):
    cont = {}
    for e in enderecos:
        t = norm(e.get("tratamento") or e.get("vocativo"))
        if "SENHORA" in t:
            g = "F"
        elif "SENHOR" in t:
            g = "M"
        else:
            continue
        p = primeiro_nome(e.get("nome") or e.get("destinatario"))
        if p:
            cont.setdefault(p, {"F": 0, "M": 0})[g] += 1
    return {p: ("F" if c["F"] > c["M"] else "M") for p, c in cont.items() if c["F"] != c["M"]}


def genero(nome, aprendido):
    p = primeiro_nome(nome)
    if p in aprendido:
        return aprendido[p], False
    if p in FEM:
        return "F", True
    if p in MASC:
        return "M", True
    if p.endswith("A") or p.endswith(FEM_FIM):
        return "F", True
    return "M", True


# ---------------------------------------------------------------- Firebase
def achar_credenciais():
    """Procura o que o tjgo_sync.py usa: conta de serviço .json ou e-mail/senha."""
    jsons, email, senha, chave = [], os.environ.get("TJGO_EMAIL"), os.environ.get("TJGO_SENHA"), None
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        jsons.append(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
    for d in PASTAS_CRED:
        if not os.path.isdir(d):
            continue
        jsons += glob.glob(os.path.join(d, "*.json"))
        for arq in glob.glob(os.path.join(d, "*.py")) + glob.glob(os.path.join(d, "*.env")) + \
                glob.glob(os.path.join(d, "*.ini")) + glob.glob(os.path.join(d, "*.txt")) + \
                glob.glob(os.path.join(d, ".env")):
            if os.path.basename(arq) == os.path.basename(__file__):
                continue
            try:
                txt = open(arq, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in re.finditer(r"[\"']([^\"'\n]+\.json)[\"']", txt):
                jsons.append(os.path.join(d, os.path.expandvars(os.path.expanduser(m.group(1)))))
            if not email:
                m = re.search(r"(?i)(?:email|e_mail|usuario|login|user)\w*[\"']?\s*[:=]\s*[\"']?"
                              r"([^\"'\s,]+@[^\"'\s,]+)", txt)
                email = m and m.group(1)
            if not senha:
                m = re.search(r"(?i)(?:senha|password|passwd|pass|pwd)\w*[\"']?\s*[:=]\s*[\"']([^\"'\n]+)[\"']", txt) \
                    or re.search(r"(?im)^\s*(?:senha|password|pass|pwd)\w*\s*=\s*([^\s\"']+)\s*$", txt)
                senha = m and m.group(1)
            if not chave:
                m = re.search(r"AIza[0-9A-Za-z_\-]{35}", txt)
                chave = m and m.group(0)
    conta = None
    for j in dict.fromkeys(jsons):
        try:
            d = json.load(open(j, encoding="utf-8"))
            if isinstance(d, dict) and d.get("type") == "service_account" and d.get("private_key"):
                conta = j
                break
        except (OSError, ValueError):
            pass
    return conta, email, senha, chave or API_KEY_SITE


class BancoAdmin:
    """Conta de serviço com firebase_admin."""

    def __init__(self, conta):
        import firebase_admin
        from firebase_admin import credentials, firestore
        self.fs = firestore
        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(conta))
        self.db = firestore.client()
        self.quem = ROBO

    def lista_tjgo(self):
        d = self.db.collection("tjgo").document("lista").get()
        return d.to_dict() if d.exists else None

    def enderecos(self):
        docs = self.db.collection("enderecos").stream()
        return [(d.id, d.to_dict()) for d in docs]

    def criar(self, docs, auditoria):
        for i in range(0, len(docs), 200):
            b = self.db.batch()
            for d in docs[i:i + 200]:
                b.set(self.db.collection("enderecos").document(),
                      {**d, "criadoEm": self.fs.SERVER_TIMESTAMP, "atualizadoEm": self.fs.SERVER_TIMESTAMP})
            b.commit()
        self.db.collection("audit").add({**auditoria, "timestamp": self.fs.SERVER_TIMESTAMP})

    def atualizar(self, doc_id, campos, auditoria):
        ref = self.db.collection("enderecos").document(doc_id)
        ref.update({**campos, "atualizadoEm": self.fs.SERVER_TIMESTAMP})
        self.db.collection("audit").add({**auditoria, "timestamp": self.fs.SERVER_TIMESTAMP})


class BancoRest:
    """E-mail e senha do sistema, pela API REST (só biblioteca padrão)."""

    BASE = f"https://firestore.googleapis.com/v1/projects/{PROJETO}/databases/(default)/documents"

    def __init__(self, email, senha, chave):
        r = self._req("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + chave,
                      {"email": email, "password": senha, "returnSecureToken": True}, token=False)
        self.token = r["idToken"]
        self.quem = f"{ROBO} - {email}"
        self.email = email

    def _req(self, url, corpo=None, token=True, method=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + self.token
        if method is None:
            method = "POST" if corpo is not None else "GET"
        req = urllib.request.Request(url, data=json.dumps(corpo).encode() if corpo is not None else None,
                                     headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Firebase respondeu {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")

    @staticmethod
    def _de(v):
        if "stringValue" in v: return v["stringValue"]
        if "booleanValue" in v: return v["booleanValue"]
        if "integerValue" in v: return int(v["integerValue"])
        if "doubleValue" in v: return v["doubleValue"]
        if "mapValue" in v: return {k: BancoRest._de(x) for k, x in v["mapValue"].get("fields", {}).items()}
        if "arrayValue" in v: return [BancoRest._de(x) for x in v["arrayValue"].get("values", [])]
        if "timestampValue" in v: return v["timestampValue"]
        return None

    def lista_tjgo(self):
        try:
            d = self._req(self.BASE + "/tjgo/lista")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise
        return {k: self._de(v) for k, v in d.get("fields", {}).items()}

    def enderecos(self):
        out, tok = [], ""
        while True:
            r = self._req(self.BASE + "/enderecos?pageSize=300" + (f"&pageToken={tok}" if tok else ""))
            for d in r.get("documents", []):
                doc_id = d["name"].rsplit("/", 1)[-1]
                campos = {k: self._de(v) for k, v in d.get("fields", {}).items()}
                out.append((doc_id, campos))
            tok = r.get("nextPageToken")
            if not tok:
                return out

    def _write(self, colecao, dados, campo_hora):
        nome = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(20))
        campos = {k: ({"booleanValue": v} if isinstance(v, bool) else {"stringValue": str(v)})
                  for k, v in dados.items()}
        return {"update": {"name": f"projects/{PROJETO}/databases/(default)/documents/{colecao}/{nome}",
                           "fields": campos},
                "currentDocument": {"exists": False},
                "updateTransforms": [{"fieldPath": c, "setToServerValue": "REQUEST_TIME"} for c in campo_hora]}

    def criar(self, docs, auditoria):
        writes = [self._write("enderecos", d, ["criadoEm", "atualizadoEm"]) for d in docs]
        for i in range(0, len(writes), 200):
            self._req(self.BASE + ":commit", {"writes": writes[i:i + 200]})
        auditoria = {**auditoria, "email": self.email}
        self._req(self.BASE + ":commit", {"writes": [self._write("audit", auditoria, ["timestamp"])]})

    def atualizar(self, doc_id, campos, auditoria):
        """Atualiza campos específicos de um documento via PATCH."""
        field_names = list(campos.keys()) + ["atualizadoEm"]
        mask = "&".join(f"updateMask.fieldPaths={f}" for f in field_names)
        url = f"{self.BASE}/enderecos/{doc_id}?{mask}"
        fs_campos = {k: ({"booleanValue": v} if isinstance(v, bool) else {"stringValue": str(v)})
                     for k, v in campos.items()}
        body = {"fields": fs_campos}
        self._req(url, body, method="PATCH")
        # Atualiza atualizadoEm via transform
        transform = {
            "writes": [{
                "transform": {
                    "document": f"projects/{PROJETO}/databases/(default)/documents/enderecos/{doc_id}",
                    "fieldTransforms": [{"fieldPath": "atualizadoEm", "setToServerValue": "REQUEST_TIME"}]
                }
            }]
        }
        self._req(self.BASE + ":commit", transform)
        auditoria = {**auditoria, "email": self.email}
        self._req(self.BASE + ":commit", {"writes": [self._write("audit", auditoria, ["timestamp"])]})


def conectar():
    conta, email, senha, chave = achar_credenciais()
    if conta:
        try:
            b = BancoAdmin(conta)
            log(f"Conectado ao Firebase com a conta de serviço {os.path.basename(conta)}")
            return b
        except ImportError:
            log("Achei a conta de serviço, mas o firebase_admin não está instalado neste Python.")
    if email and senha:
        b = BancoRest(email, senha, chave)
        log(f"Conectado ao Firebase como {email}")
        return b
    raise RuntimeError("Não achei as credenciais do Firebase (conta de serviço .json ou e-mail/senha) "
                       "em: " + "; ".join(PASTAS_CRED))


# ---------------------------------------------------------------- comarca update helpers
def extrair_comarca_do_cargo(cargo):
    """Extrai o nome da comarca do campo cargo, ex: 'Juíza de Direito da Comarca de Posse' → 'Posse'."""
    m = re.search(r"(?i)comarca\s+de\s+(.+)$", str(cargo or ""))
    if m:
        return m.group(1).strip()
    return None


def comarca_mudou(cargo_existente, nova_comarca):
    """Retorna True se a comarca no cargo existente difere da nova comarca (comparação normalizada)."""
    comarca_antiga = extrair_comarca_do_cargo(cargo_existente)
    if not comarca_antiga:
        return False
    return norm(comarca_antiga) != norm(nova_comarca)


def montar_cargo_atualizado(cargo_existente, comarca_antiga, nova_comarca_titulo):
    """Substitui o nome da comarca no cargo, preservando o restante do texto."""
    # Localiza "Comarca de NOME_ANTIGO" e substitui pelo novo nome
    return re.sub(
        r"(?i)(comarca\s+de\s+)" + re.escape(comarca_antiga),
        lambda m: m.group(1) + nova_comarca_titulo,
        cargo_existente,
        count=1
    )


def montar_localidade_atualizada(localidade_existente, nova_cidade_titulo):
    """Atualiza a localidade se ela contém o nome antigo da comarca no padrão 'CIDADE – GO'.
    Não altera se for 'NESTA' ou variantes."""
    loc = str(localidade_existente or "")
    # Não altera se for "NESTA" ou "N E S T A"
    if re.match(r"(?i)^n\s*e\s*s\s*t\s*a$", loc.strip()):
        return loc
    # Atualiza para nova cidade no formato "CIDADE – GO" (cidade em maiúsculas)
    nova_cidade_upper = nova_cidade_titulo.upper()
    return f"{nova_cidade_upper} – GO"


# ---------------------------------------------------------------- principal
def linhas_tjgo(banco):
    try:
        lista = banco.lista_tjgo()
    except Exception as e:
        lista = None
        log(f"Não consegui ler tjgo/lista no Firebase ({e}); uso o tjgo_tabela.json.")
    if lista and lista.get("linhas"):
        log(f"Lista do TJGO (Firebase) de {lista.get('atualizacaoSite') or '?'}: {len(lista['linhas'])} linhas")
        return lista["linhas"]
    d = json.load(open(TABELA, encoding="utf-8"))
    log(f"Lista do TJGO (tjgo_tabela.json de {d.get('quando')}): {len(d['rows'])} linhas")
    return [{"comarca": r[1], "vara": r[2], "pessoa": r[4]} for r in d["rows"] if len(r) >= 5]


def montar(l, aprendido):
    nome = titular_limpo(l.get("pessoa")).upper()
    g, estimado = genero(nome, aprendido)
    cidade = titulo(comarca_curta(l.get("comarca")))
    vara = vara_limpa(l.get("vara"))
    juiz = "Juíza de Direito" if g == "F" else "Juiz de Direito"
    cargo = f"{juiz} {artigo(vara)} {vara}" if vara else juiz
    local = "N E S T A" if norm(cidade) == "GOIANIA" else f"{cidade} – GO"
    return {
        "tipo": "simples",
        "tratamento": "A Sua Excelência a Senhora" if g == "F" else "A Sua Excelência o Senhor",
        "vocativo": "Senhora Juíza de Direito," if g == "F" else "Senhor Juiz de Direito,",
        "nome": nome, "destinatario": nome, "cargo": cargo,
        "orgao": f"Comarca de {cidade}", "local": local, "localData": local,
        "tjgoComarca": l.get("comarca") or "", "tjgoVara": l.get("vara") or "",
        "tjgoSemVinculo": False, "origem": "automatico",
    }, estimado


def main():
    log("=" * 60 + (" (SIMULAÇÃO)" if SIMULAR else ""))
    banco = conectar()
    linhas = linhas_tjgo(banco)
    ends_raw = banco.enderecos()
    # ends_raw is list of (doc_id, dict)
    ends = [campos for _, campos in ends_raw]
    log(f"Endereçamentos cadastrados: {len(ends)}")

    # Índice: nome normalizado → (doc_id, campos)
    por_nome = {}
    for doc_id, campos in ends_raw:
        for k in ("nome", "destinatario"):
            n = norm(campos.get(k) or "")
            if n and len(n) >= 8:
                por_nome.setdefault(n, (doc_id, campos))

    textos = [" " + norm(" ".join(str(e.get(k) or "") for k in ("nome", "destinatario"))) + " " for e in ends]
    vinculados = {}
    for e in ends:
        if e.get("tjgoComarca"):
            k = (e.get("tjgoComarca"), e.get("tjgoVara") or "")
            vinculados[k] = vinculados.get(k, 0) + 1
    titulares_vara = {}
    for l in linhas:
        k = (l.get("comarca"), l.get("vara") or "")
        titulares_vara[k] = titulares_vara.get(k, 0) + 1

    aprendido = aprender_generos(ends)
    vistos, novos, pulados = set(), [], []
    atualizacoes = []  # list of (doc_id, campos_atuais, nova_comarca_titulo, nome_log, comarca_antiga_str)

    for l in linhas:
        p = norm(titular_limpo(l.get("pessoa")))
        if re.match(r"^VAG|SEM TITULAR", p) or len(p) < 8 or p in vistos:
            continue
        vistos.add(p)
        nova_comarca = comarca_curta(l.get("comarca"))
        nova_cidade = titulo(nova_comarca)

        # Verifica se já existe endereçamento pelo nome
        encontrado = any(" " + p + " " in t for t in textos)
        if encontrado:
            # Verificar se a comarca mudou
            match = por_nome.get(p)
            if match:
                doc_id, campos = match
                cargo_existente = campos.get("cargo") or ""
                comarca_antiga = extrair_comarca_do_cargo(cargo_existente)
                if comarca_antiga and comarca_mudou(cargo_existente, nova_cidade):
                    atualizacoes.append((doc_id, campos, nova_cidade, p, comarca_antiga))
                # else: sem alteração
            continue

        k = (l.get("comarca"), l.get("vara") or "")
        if vinculados.get(k, 0) >= titulares_vara.get(k, 1):
            # A vara já tem endereçamento vinculado com outro nome: é troca de titular,
            # que o sistema atualiza pelo aviso do TJGO. Criar aqui duplicaria.
            pulados.append(f"{l.get('pessoa')} ({comarca_curta(k[0])} / {k[1] or 'vara única'})")
            continue
        novos.append(montar(l, aprendido))

    criados = len(novos)
    atualizados = 0
    sem_alteracao = len(vistos) - criados - len(pulados) - len(atualizacoes)
    erros = 0

    log(f"Titulares na lista: {len(vistos)}. Sem endereçamento: {criados + len(pulados)}. "
        f"Com comarca mudada: {len(atualizacoes)}.")
    for s in pulados:
        log(f"  PULADO (vara já tem endereçamento vinculado; use o aviso do TJGO no sistema): {s}")

    # --- Criar faltantes ---
    if not novos:
        log("Nada a criar.")
    elif len(novos) > LIMITE and not FORCAR:
        log(f"PARADO: {len(novos)} a criar passa do limite de segurança ({LIMITE}). "
            "Confira a lista e rode com --forcar se estiver certo.")
    else:
        for d, est in novos:
            log(f"  {'(simulação) ' if SIMULAR else ''}CRIAR: {d['nome']} | {d['cargo']} | {d['orgao']}"
                + ("   [conferir Senhor/Senhora]" if est else ""))
        if not SIMULAR:
            docs = [d for d, _ in novos]
            banco.criar(docs, {"usuario": banco.quem, "email": "", "acao": "Criação Automática Endereçamento",
                               "detalhes": f"Criou {len(docs)} endereçamento(s) faltante(s) do TJGO: "
                                           + ", ".join(d["nome"] for d in docs)[:1400]})
            log(f"OK: {len(docs)} endereçamento(s) criado(s).")
            atualizados += len(docs)
            criados = len(docs)

    # --- Atualizar comarca ---
    for doc_id, campos_atuais, nova_cidade, nome_norm, comarca_antiga in atualizacoes:
        cargo_existente = campos_atuais.get("cargo") or ""
        localidade_existente = campos_atuais.get("localidade") or campos_atuais.get("local") or ""

        novo_cargo = montar_cargo_atualizado(cargo_existente, comarca_antiga, nova_cidade)
        nova_localidade = montar_localidade_atualizada(localidade_existente, nova_cidade)
        # Se for Goiânia, usar "N E S T A"
        if norm(nova_cidade) == "GOIANIA":
            nova_localidade = "N E S T A"

        campos_novos = {"cargo": novo_cargo}
        # Atualiza localidade somente se havia valor e não é "NESTA"
        if localidade_existente and not re.match(r"(?i)^n\s*e\s*s\s*t\s*a$", localidade_existente.strip()):
            campos_novos["localidade"] = nova_localidade
        # Também atualiza local/localData se existirem e não forem NESTA
        for campo_loc in ("local", "localData"):
            val = campos_atuais.get(campo_loc) or ""
            if val and not re.match(r"(?i)^n\s*e\s*s\s*t\s*a$", val.strip()):
                campos_novos[campo_loc] = nova_localidade

        nome_display = campos_atuais.get("nome") or nome_norm
        log(f"  {'(simulação) ' if SIMULAR else ''}Atualizado: {nome_display} | "
            f"Comarca: {comarca_antiga} → {nova_cidade}")
        log(f"    cargo: {cargo_existente!r} → {novo_cargo!r}")

        if not SIMULAR:
            try:
                banco.atualizar(doc_id, campos_novos,
                                {"usuario": banco.quem, "email": "",
                                 "acao": "Atualização Automática Comarca",
                                 "detalhes": f"Comarca: {comarca_antiga} → {nova_cidade} | {nome_display}"})
                atualizados += 1
            except Exception as e:
                log(f"    ERRO ao atualizar {nome_display}: {e}")
                erros += 1

    if SIMULAR:
        atualizados = len(atualizacoes)

    log(f"Resultado: criados={criados}, atualizados={atualizados}, "
        f"sem_alteracao={sem_alteracao}, erros={erros}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERRO:\n" + traceback.format_exc())
        raise SystemExit(1)
