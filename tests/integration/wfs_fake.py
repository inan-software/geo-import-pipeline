"""A minimal paging WFS and self-written test features."""

import httpx

NS = (
    'xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" '
    'xmlns:app="http://example.org/app"'
)


def square(x):
    return f"{x} 5800000 {x + 100} 5800000 {x + 100} 5800100 {x} 5800100 {x} 5800000"


def bowtie(x):
    return f"{x} 5800000 {x + 100} 5800100 {x + 100} 5800000 {x} 5800100 {x} 5800000"


def feature(key, name, coords, gml_id=None):
    geom = (
        '<gml:MultiSurface srsName="urn:ogc:def:crs:EPSG::25833"><gml:surfaceMember><gml:Polygon><gml:exterior>'
        f"<gml:LinearRing><gml:posList>{coords}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>"
        "</gml:surfaceMember></gml:MultiSurface>"
    )
    return (
        f'<wfs:member><app:area gml:id="{gml_id or "f" + key}"><app:code>{key}</app:code>'
        f"<app:label>{name}</app:label><app:geom>{geom}</app:geom></app:area></wfs:member>"
    )


class FakeWfs:
    def __init__(self, features, hits=None):
        self.features = features
        self.hits = len(features) if hits is None else hits
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if params.get("RESULTTYPE") == "hits":
            return httpx.Response(200, text=f'<wfs:FeatureCollection {NS} numberMatched="{self.hits}"/>')
        start, count = int(params["STARTINDEX"]), int(params["COUNT"])
        members = "".join(self.features[start : start + count])
        return httpx.Response(
            200, text=f'<wfs:FeatureCollection {NS} numberMatched="unknown">{members}</wfs:FeatureCollection>'
        )
