"""
API de cálculo de volume — recebe fotos (processa via NodeODM) OU um
arquivo .tif/DEM já pronto (Metashape, WebODM, QGIS, etc) e calcula o
volume pelo método de corte/aterro, o mesmo já validado no protótipo
com câmera de profundidade.

Endpoints:
  POST /volume/de-fotos    -> multipart, campo 'fotos' (múltiplas) + 'densidade'
  POST /volume/de-tif      -> multipart, campo 'tif' (1 arquivo) + 'densidade'
                               (usa o percentil mais baixo do próprio DEM como
                               "nível do chão" -- aproximação. Ver observação
                               no README sobre por que 'dois-tifs' é mais preciso)
  POST /volume/dois-tifs   -> multipart, campos 'baseline' e 'carregado' + 'densidade'
                               (o método mais preciso: dois DEMs, vazio vs cheio,
                               igual ao pipeline da câmera de profundidade)
  GET  /health
"""

import os
import time

import numpy as np
import rasterio
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Libera CORS: a interface (porta 8080) e a API (porta 5000) são
# origens diferentes do ponto de vista do navegador, então sem isso o
# fetch() é bloqueado com "NetworkError when attempting to fetch
# resource" (Firefox) ou "Failed to fetch" (Chrome) mesmo com a API
# rodando normalmente.
@app.after_request
def liberar_cors(resposta):
    resposta.headers["Access-Control-Allow-Origin"] = "*"
    resposta.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resposta.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resposta


@app.route("/<path:qualquer>", methods=["OPTIONS"])
def preflight(qualquer):
    return "", 204


NODEODM_URL = os.environ.get("NODEODM_URL", "http://nodeodm:3000")
PASTA_DADOS = "/dados"
DENSIDADE_PADRAO = 900  # kg/m3


RESOLUCAO_MAX_VISUALIZACAO = 80  # limita o payload JSON devolvido ao navegador


def _grade_para_visualizacao(diferenca_m, gsd_x, gsd_y, max_resolucao=RESOLUCAO_MAX_VISUALIZACAO):
    """
    Reduz a grade de diferença de altura para no máximo NxN células,
    pra poder mandar pro navegador via JSON sem pesar demais (um DEM
    real facilmente tem milhões de pixels; a pilha visual não precisa
    de mais que ~80x80 pra ficar reconhecível na tela).
    """
    from scipy.ndimage import zoom

    altura, largura = diferenca_m.shape
    fator_y = min(1.0, max_resolucao / altura)
    fator_x = min(1.0, max_resolucao / largura)
    reduzida = zoom(diferenca_m, (fator_y, fator_x), order=1) if (fator_y < 1 or fator_x < 1) else diferenca_m

    n_y, n_x = reduzida.shape
    return {
        "grade": reduzida.round(4).tolist(),
        "largura_m": round(largura * gsd_x, 3),
        "comprimento_m": round(altura * gsd_y, 3),
    }


# ----------------------------------------------------------------------
# CAMINHO 1: fotos brutas -> NodeODM processa -> DEM -> volume
# ----------------------------------------------------------------------
@app.route("/volume/de-fotos", methods=["POST"])
def volume_de_fotos():
    fotos = request.files.getlist("fotos")
    densidade = float(request.form.get("densidade", DENSIDADE_PADRAO))
    if not fotos:
        return jsonify({"erro": "nenhuma foto enviada (campo 'fotos')"}), 400
    if len(fotos) < 5:
        return jsonify({"erro": "fotogrametria precisa de pelo menos ~5 fotos com sobreposição"}), 400

    pasta_tmp = os.path.join(PASTA_DADOS, f"voo_{int(time.time())}")
    os.makedirs(pasta_tmp, exist_ok=True)

    caminhos = []
    for foto in fotos:
        caminho = os.path.join(pasta_tmp, foto.filename)
        foto.save(caminho)
        caminhos.append(caminho)

    try:
        caminho_dem = _processar_no_nodeodm(caminhos, pasta_tmp)
    except Exception as e:
        return jsonify({"erro": f"falha no processamento fotogramétrico: {e}"}), 502

    try:
        resultado = _volume_de_um_dem(caminho_dem, densidade)
    except Exception as e:
        return jsonify({"erro": f"DEM gerado, mas falhou ao calcular volume: {e}"}), 422

    resultado["dem_gerado_em"] = caminho_dem
    return jsonify(resultado)


def _processar_no_nodeodm(caminhos_fotos, pasta_saida):
    resp = requests.post(f"{NODEODM_URL}/task/new/init")
    resp.raise_for_status()
    uuid = resp.json()["uuid"]

    for caminho in caminhos_fotos:
        with open(caminho, "rb") as f:
            requests.post(f"{NODEODM_URL}/task/new/upload/{uuid}", files={"images": f})

    requests.post(f"{NODEODM_URL}/task/new/commit/{uuid}")

    # polling -- fotogrametria de verdade leva de minutos a horas
    # dependendo do nº de fotos e da resolução escolhida
    while True:
        info = requests.get(f"{NODEODM_URL}/task/{uuid}/info").json()
        codigo = info["status"]["code"]
        if codigo == 40:  # COMPLETED
            break
        if codigo in (30, 50):  # FAILED / CANCELED
            raise RuntimeError(f"status retornado: {info['status']}")
        time.sleep(10)

    dem_resp = requests.get(f"{NODEODM_URL}/task/{uuid}/download/dem.tif")
    dem_resp.raise_for_status()
    caminho_dem = os.path.join(pasta_saida, "dem.tif")
    with open(caminho_dem, "wb") as f:
        f.write(dem_resp.content)
    return caminho_dem


# ----------------------------------------------------------------------
# CAMINHO 2: .tif já processado (Metashape, WebODM, QGIS...) -> volume
# ----------------------------------------------------------------------
@app.route("/volume/de-tif", methods=["POST"])
def volume_de_tif():
    """
    Aceita um único DEM/DSM .tif e estima o volume acima do 'chão',
    aproximado como o percentil 2 das cotas do próprio arquivo.

    Limitação: essa aproximação assume que a maior parte da área
    escaneada é chão vazio ao redor da pilha. Se o .tif cobrir só a
    pilha (sem margem de chão visível), use /volume/dois-tifs, que
    é o método correto e mais preciso.
    """
    arquivo = request.files.get("tif")
    densidade = float(request.form.get("densidade", DENSIDADE_PADRAO))
    if not arquivo:
        return jsonify({"erro": "nenhum arquivo enviado (campo 'tif')"}), 400

    os.makedirs(PASTA_DADOS, exist_ok=True)
    caminho_tmp = os.path.join(PASTA_DADOS, f"upload_{int(time.time())}.tif")
    arquivo.save(caminho_tmp)

    try:
        resultado = _volume_de_um_dem(caminho_tmp, densidade)
    except Exception as e:
        return jsonify({"erro": f"não consegui ler/processar o arquivo: {e}"}), 422

    return jsonify(resultado)


def _volume_de_um_dem(caminho_tif, densidade):
    with rasterio.open(caminho_tif) as src:
        dem = src.read(1).astype(np.float64)
        gsd_x = abs(src.transform[0])
        gsd_y = abs(src.transform[4])
        if src.nodata is not None:
            dem = np.where(dem == src.nodata, np.nan, dem)

    valido = ~np.isnan(dem)
    if not valido.any():
        raise ValueError("arquivo não tem dados de elevação válidos")

    nivel_base = float(np.nanpercentile(dem[valido], 2))
    diferenca = np.nan_to_num(np.clip(dem - nivel_base, 0, None), nan=0.0)

    area_celula = gsd_x * gsd_y
    volume_m3 = float(np.sum(diferenca) * area_celula)
    massa_kg = volume_m3 * densidade

    resultado = {
        "volume_m3": round(volume_m3, 4),
        "massa_kg": round(massa_kg, 1),
        "gsd_m": round(gsd_x, 4),
        "nivel_base_m": round(nivel_base, 3),
        "metodo": "percentil-do-proprio-dem (aproximado)",
    }
    resultado.update(_grade_para_visualizacao(diferenca, gsd_x, gsd_y))
    return resultado


# ----------------------------------------------------------------------
# CAMINHO 3 (mais preciso): dois DEMs -- baseline vazio + carregado
# ----------------------------------------------------------------------
@app.route("/volume/dois-tifs", methods=["POST"])
def volume_de_dois_tifs():
    baseline = request.files.get("baseline")
    carregado = request.files.get("carregado")
    densidade = float(request.form.get("densidade", DENSIDADE_PADRAO))
    if not baseline or not carregado:
        return jsonify({"erro": "envie os dois arquivos: 'baseline' e 'carregado'"}), 400

    os.makedirs(PASTA_DADOS, exist_ok=True)
    ts = int(time.time())
    caminho_base = os.path.join(PASTA_DADOS, f"base_{ts}.tif")
    caminho_carr = os.path.join(PASTA_DADOS, f"carr_{ts}.tif")
    baseline.save(caminho_base)
    carregado.save(caminho_carr)

    try:
        with rasterio.open(caminho_base) as src:
            dem_base = src.read(1).astype(np.float64)
            gsd_x = abs(src.transform[0])
            gsd_y = abs(src.transform[4])
        with rasterio.open(caminho_carr) as src:
            dem_carr = src.read(1).astype(np.float64)

        if dem_base.shape != dem_carr.shape:
            from scipy.ndimage import zoom
            fator = (dem_base.shape[0] / dem_carr.shape[0],
                      dem_base.shape[1] / dem_carr.shape[1])
            dem_carr = zoom(dem_carr, fator, order=1)
    except Exception as e:
        return jsonify({"erro": f"não consegui ler os arquivos: {e}"}), 422

    diferenca = np.nan_to_num(np.clip(dem_carr - dem_base, 0, None), nan=0.0)
    area_celula = gsd_x * gsd_y
    volume_m3 = float(np.sum(diferenca) * area_celula)
    massa_kg = volume_m3 * densidade

    resultado = {
        "volume_m3": round(volume_m3, 4),
        "massa_kg": round(massa_kg, 1),
        "gsd_m": round(gsd_x, 4),
        "metodo": "diferenca-de-dois-dems (preciso)",
    }
    resultado.update(_grade_para_visualizacao(diferenca, gsd_x, gsd_y))
    return jsonify(resultado)


if __name__ == "__main__":
    os.makedirs(PASTA_DADOS, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
