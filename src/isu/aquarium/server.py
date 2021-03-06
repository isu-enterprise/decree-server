from __future__ import print_function
from pyramid.view import view_config
from pyramid.config import Configurator
from pkg_resources import resource_filename
import pymorphy2
from pymorphy2.tokenizers import simple_word_tokenize
# from icc.mvw.interfaces import IView
from isu.aquarium import russian
from lxml import html
from lxml import etree
import os
import os.path
import rdflib
from pyramid.renderers import render
import json
import pprint
from collections import namedtuple

from elasticsearch import Elasticsearch

#ES = Elasticsearch(['localhost'], http_auth=("elastic", "elastic321"))
ES = Elasticsearch(['localhost'])
INDEX = "aquarium"

morpher = pymorphy2.MorphAnalyzer()
BASE_URL = "http://irnok.net:8080/"  # + UUID + ".xhtml"


def asBaseURL(uuid):
    return BASE_URL + uuid + ".xhtml"


SAVE_DIR = resource_filename("isu.aquarium", "documents")


class DocumentData(object):

    def __init__(self, request):
        if hasattr(request, "matchdict"):
            if "document_uuid" in request.matchdict:
                return self.load(uuid=request.matchdict["document_uuid"])
        return self.get_body_data(request)

    def load(self, uuid):
        i = open(self.filename(uuid), "r", encoding="utf-8")
        self.body = i.read()
        i.close()

    def get_body_data(self, request):
        self.body = request.body.decode("utf8")
        self.xml = html.fromstring(self.body)
        root = self.root = self.xml.xpath('//*[@id="main-document"]')[0]
        self.resource = root.get("resource", None)
        self.uuid = root.get("data-uuid", None)
        self.id = root.get("id", None)

    def filename(self, uuid=None):
        if uuid is None:
            uuid = self.uuid
        if not os.path.isdir(SAVE_DIR):
            os.makedirs(SAVE_DIR)
        return os.path.join(SAVE_DIR, uuid + ".xhtml")

    def save_body(self, overwite):
        if self.resource is not None:
            filename = self.filename()
            if not overwite and os.path.isfile(filename):
                return {"result": "KO", "error": "Document already exists!"}
            # Ok, we must save the content
            o = open(filename, "w", encoding="utf-8")
            o.write(self.body)
            o.close()
            self.index()
            return {"result": "OK", "error": "Document successfully saved!"}
        else:
            return {"result": "KO", "error": "Resource name is not found!"}

    def index(self):
        url = asBaseURL(self.uuid)
        txt = etree.tostring(self.xml, encoding=str, pretty_print=True)
        result = render("isu.aquarium:templates/editor.pt", {"content": txt})

        open("document.xhtml", "w", encoding="utf-8").write(result)

        g = rdflib.Graph()
        g.parse(data=result, publicID=url, format='rdfa')
        ser = g.serialize(format='json-ld')

        open("document.jsonld", "wb").write(ser)
        open("document.ttl", "wb").write(g.serialize(format="ttl"))

        filename = self.filename().replace(".xhtml", ".jsonld")
        o = open(filename, "wb")
        o.write(ser)
        o.close()
        js = json.loads(ser)
        graph = {"graph": js}
        rc = ES.index(
            index=INDEX,
            doc_type="document",
            id=self.uuid,
            body=graph,
            refresh=True)
        # print ("Elastic:{}".format(rc))


@view_config(
    route_name='document', renderer="isu.aquarium:templates/editor.pt")
def document(request):
    doc = DocumentData(request)
    return {"content": doc.body}

Row = namedtuple('Row', ['id', 'docname', 'personname'])


class SearchView(object):

    def __init__(self, rc):
        self.rc = rc
        self.G = rdflib.Graph()
        self.load_graphs()

    @property
    def total(self):
        return int(self.rc["hits"]["total"])

    @property
    def empty(self):
        return self.total == 0

    @property
    def graphs(self):
        for g in self.rc["hits"]["hits"]:
            yield g["_source"]["graph"]

    def load_graphs(self):
        for g in self.graphs:
            self.G.parse(data=json.dumps(g), format='json-ld')

    @property
    def items(self):
        Q = """
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>
PREFIX dct: <http://purl.org/dc/terms/>
PREFIX bibo: <http://purl.org/ontology/bibo/>
PREFIX oa: <http://www.w3.org/ns/oa#>
SELECT ?ID ?doc_name ?person_name WHERE {
  ?doc a foaf:Document.
  ?doc dct:title ?doc_name .
  ?doc oa:id ?ID .
  ?doc bibo:owner ?p .
  ?p a foaf:Person .
  ?p foaf:name ?person_name .
}
LIMIT 10
        """
        for id, d, p in self.G.query(Q):
            yield Row(id=id, docname=d, personname=p)


@view_config(
    route_name="search", renderer="isu.aquarium:templates/search.pt"
)
def search(request):
    form = ""
    query = request.POST.get("q", None)
    if query is not None:
        query = query.strip()
        if query:
            form += "<br/>Query: {}</br>".format(query)
            rc = ES.search(index=INDEX, doc_type="document",
                           # body={"query": {"match_all": {}}}
                           # body={"query": {"match": {"@value": query}}}
                           body={"query": {"query_string": {"query": query}}}
                           )
            view = SearchView(rc)
            for i in view.items:
                pprint.pprint(i)

            for hit in rc['hits']['hits']:
                _id = hit["_id"]
                form += "<a href=\"{}.xhtml\">{}</a><br/>".format(_id, _id)
    else:
        query = ""
        view = None
    return {"content": form, "query": query, "view": view}


def lean(word, case):
    vals = morpher.parse(word)
    for v in vals:
        if 'NOUN' in v.tag:
            inflection = set(case.split())
            answer = v.inflect(inflection).word
            print(inflection, v, answer)
            return answer
    else:
        return ("<strong>Для слова {} не найден "
                "вариант существительного!</strong>".format(v))


TR = {
    "nomn": "N",
    "gent": "G",
    "datv": "D",
    "accs": "A",
    "ablt": "T",
    "loct": "P",
}


@view_config(route_name="api-morphy", renderer='json', request_method="POST")
def api_morphy(request):
    query = request.json_body
    command = query["command"]
    phrase = query["phrase"]
    if command == "all":
        words = simple_word_tokenize(phrase)
        gender = russian.gender(words, "M", "F", "_")
        if gender == "_":
            new_phrase = []
            for word in words:
                new_phrase.append(lean(word, case=query["case"]))
        else:
            case_ = query["case"]
            c = TR.get(case_, case_)
            new_phrase = russian.make_human_case(words, c)
        return {"phrase": " ".join(new_phrase)}
    elif command == "gender":
        p = russian.gender(phrase, query["M"], query["F"], ";-)")
        return {"phrase": p}


@view_config(route_name="api-save-as", renderer='json', request_method="POST")
def api_save_as(request):
    doc = DocumentData(request)
    return doc.save_body(overwite=False)


@view_config(route_name="api-save", renderer='json', request_method="POST")
def api_save(request):
    doc = DocumentData(request)
    return doc.save_body(overwite=True)


def static_path(dir):
    return 'isu.aquarium:' + 'client/' + dir


def main(config, **settings):
    config = Configurator(settings=settings)
    config.add_route('document', '/{document_uuid}.xhtml')
    config.add_route('search', '/')
    config.add_route('api-morphy', '/api/morphy')
    config.add_route('api-save', '/api/save')
    config.add_route('api-save-as', '/api/save-as')

    for asset in """assets css dist fonts images js""".split():
        config.add_static_view(name=asset, path=static_path(asset))

    config.scan()
    app = config.make_wsgi_app()
    return app
