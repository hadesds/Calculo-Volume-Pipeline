"""
Protótipo: cálculo de volume via fotogrametria de drone (DEM).

Visão de longo prazo do projeto: em vez da câmera de profundidade fixa
acima do box, um drone sobrevoa a área e captura fotos aéreas com
sobreposição. Um software de fotogrametria (Agisoft Metashape ou
WebODM/NodeODM) processa essas fotos e gera:
  - uma ortofoto (mosaico de imagem georreferenciado)
  - um DEM -- Modelo Digital de Elevação -- que é, na prática, um
    heightmap georreferenciado. Estruturalmente é o MESMO dado que já
    usávamos vindo da câmera de profundidade.

O cálculo de volume não muda: é o mesmo método de corte/aterro
(diferença de altura x área da célula, somado) que qualquer ferramenta
de volumetria em DEM usa por baixo (inclusive as calculadoras de
"stockpile volume" do QGIS, Metashape e WebODM).

Este arquivo prototipa 4 partes:
  1. Planejamento de voo (trajeto em zigue-zague sobre o box)
  2. Simulação do DEM que sairia do processamento fotogramétrico
  3. Cálculo de volume por corte/aterro (idêntico ao pipeline da câmera)
  4. Stub de integração real com a API do NodeODM/WebODM

Requisitos p/ prototipagem (sem hardware real):
    pip install numpy scipy plotly --break-system-packages
Requisitos p/ integração real (quando tiver o drone/imagens):
    pip install requests rasterio --break-system-packages
    (rasterio depende de GDAL -- se a wheel não existir pra sua versão
    de Python, use conda: conda install -c conda-forge rasterio)
"""

import numpy as np
from dataclasses import dataclass
from scipy.ndimage import median_filter


# ----------------------------------------------------------------------
# 1. PLANEJAMENTO DE VOO
# ----------------------------------------------------------------------
@dataclass
class PlanoDeVoo:
    area_largura_m: float
    area_comprimento_m: float
    altitude_m: float = 15.0
    sobreposicao_frontal: float = 0.80    # 80% é o padrão em fotogrametria
    sobreposicao_lateral: float = 0.70    # 70% é o padrão em fotogrametria
    fov_horizontal_graus: float = 84.0    # FOV típico de drone (ex: DJI Mavic)

    def largura_coberta_por_foto_m(self):
        """Quanto de terreno cabe numa foto, dado a altitude de voo."""
        return 2 * self.altitude_m * np.tan(np.radians(self.fov_horizontal_graus / 2))

    def gsd_m_por_pixel(self, largura_sensor_px=4000):
        """
        Ground Sample Distance: quantos metros cada pixel da foto
        representa no chão. Quanto menor o GSD, mais detalhe no DEM
        final -- mas voo mais baixo = mais fotos = mais tempo de
        processamento fotogramétrico.
        """
        return self.largura_coberta_por_foto_m() / largura_sensor_px

    def gerar_trajeto(self):
        """
        Gera o trajeto em zigue-zague ('lawnmower pattern'), padrão de
        missões de mapeamento aéreo (é o que o Pix4Dcapture, DroneDeploy
        e Litchi geram automaticamente). O espaçamento entre as linhas é
        calculado a partir da sobreposição lateral desejada.
        """
        largura_coberta = self.largura_coberta_por_foto_m()
        espacamento_linhas = largura_coberta * (1 - self.sobreposicao_lateral)
        n_linhas = max(2, int(np.ceil(self.area_comprimento_m / espacamento_linhas)) + 1)

        pontos = []
        for i in range(n_linhas):
            y = min(i * espacamento_linhas, self.area_comprimento_m)
            if i % 2 == 0:
                pontos.append((0.0, y))
                pontos.append((self.area_largura_m, y))
            else:
                pontos.append((self.area_largura_m, y))
                pontos.append((0.0, y))
        return pontos

    def n_fotos_estimadas(self):
        """Estimativa de quantas fotos o voo vai gerar, dado o overlap frontal."""
        largura_coberta = self.largura_coberta_por_foto_m()
        espacamento_frontal = largura_coberta * (1 - self.sobreposicao_frontal)
        n_linhas = max(2, int(np.ceil(self.area_comprimento_m / (largura_coberta * (1 - self.sobreposicao_lateral)))) + 1)
        fotos_por_linha = max(2, int(np.ceil(self.area_largura_m / espacamento_frontal)) + 1)
        return n_linhas * fotos_por_linha


# ----------------------------------------------------------------------
# 2. SIMULAÇÃO DO DEM (substitui o processamento fotogramétrico real)
# ----------------------------------------------------------------------
def simular_dem(area_largura_m, area_comprimento_m, altura_pico_m, gsd_m,
                 ruido=0.012, semente=None):
    """
    Simula o DEM que sairia do Metashape/WebODM após processar as fotos.
    Na vida real, isso vem de um arquivo .tif georreferenciado -- aqui
    geramos sinteticamente pra prototipar o fluxo inteiro sem precisar
    rodar fotogrametria de verdade (que exige as fotos + várias horas
    de processamento).

    O ruído aqui é mais alto que o da câmera de profundidade (0.012 vs
    0.003) porque reconstrução fotogramétrica de superfícies com pouca
    textura (fertilizante granulado e uniforme) tende a ser mais
    ruidosa -- isso é uma limitação real que vale mencionar no pitch.
    """
    rng = np.random.default_rng(semente)
    n_x = max(10, int(area_largura_m / gsd_m))
    n_y = max(10, int(area_comprimento_m / gsd_m))

    x = np.linspace(0, area_largura_m, n_x)
    y = np.linspace(0, area_comprimento_m, n_y)
    X, Y = np.meshgrid(x, y, indexing="ij")

    if altura_pico_m > 0:
        cx, cy = area_largura_m / 2, area_comprimento_m / 2
        raio = min(area_largura_m, area_comprimento_m) / 2.2
        dist = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
        Z = altura_pico_m * np.clip(1 - dist / raio, 0, None)
    else:
        Z = np.zeros_like(X)

    Z += rng.normal(0, ruido, Z.shape)
    Z = median_filter(Z, size=3)
    return Z, x, y


# ----------------------------------------------------------------------
# 3. CÁLCULO DE VOLUME POR CORTE/ATERRO (idêntico ao pipeline da câmera)
# ----------------------------------------------------------------------
def calcular_volume_dem(dem_vazio, dem_carregado, gsd_m, densidade_kg_m3=900):
    diferenca = np.clip(dem_carregado - dem_vazio, 0, None)
    area_celula_m2 = gsd_m ** 2
    volume_m3 = np.sum(diferenca) * area_celula_m2
    massa_kg = volume_m3 * densidade_kg_m3
    return volume_m3, massa_kg, diferenca


# ----------------------------------------------------------------------
# 4. INTEGRAÇÃO REAL: NodeODM / WebODM (stub -- requer servidor rodando)
# ----------------------------------------------------------------------
def processar_fotos_webodm(caminhos_fotos, url_node_odm="http://localhost:3000"):
    """
    Automação real do processamento fotogramétrico via API do NodeODM
    (o motor de processamento por trás do WebODM).

    Pré-requisito: um NodeODM rodando, por exemplo via Docker:
        docker run -p 3000:3000 opendronemap/nodeodm

    Fluxo (mesmo que a interface web do WebODM faz por baixo):
      1. cria a task
      2. envia cada foto
      3. inicia o processamento (commit)
      4. aguarda status COMPLETED (polling)
      5. baixa o dem.tif gerado
    """
    import time
    import requests

    resp = requests.post(f"{url_node_odm}/task/new/init")
    uuid = resp.json()["uuid"]

    for caminho in caminhos_fotos:
        with open(caminho, "rb") as f:
            requests.post(f"{url_node_odm}/task/new/upload/{uuid}",
                          files={"images": f})

    requests.post(f"{url_node_odm}/task/new/commit/{uuid}")

    while True:
        info = requests.get(f"{url_node_odm}/task/{uuid}/info").json()
        codigo_status = info["status"]["code"]
        if codigo_status == 40:   # COMPLETED
            break
        if codigo_status in (30, 50):  # FAILED / CANCELED
            raise RuntimeError(f"Processamento falhou: {info}")
        time.sleep(10)

    dem_bytes = requests.get(f"{url_node_odm}/task/{uuid}/download/dem.tif").content
    with open("dem_baixado.tif", "wb") as f:
        f.write(dem_bytes)

    return "dem_baixado.tif"


def ler_dem_tif(caminho_tif):
    """
    Lê um DEM real (.tif) exportado do WebODM ou do Metashape.
    Requer rasterio (que por sua vez requer GDAL) -- se a wheel não
    existir pra sua versão de Python, use um ambiente conda à parte
    só pra essa leitura, e passe o array numpy resultante pro resto
    do pipeline normalmente.
    """
    import rasterio

    with rasterio.open(caminho_tif) as src:
        dem = src.read(1).astype(np.float64)
        gsd_m = src.transform[0]
        if src.nodata is not None:
            dem = np.where(dem == src.nodata, np.nan, dem)
    return dem, gsd_m


# ----------------------------------------------------------------------
# DEMO
# ----------------------------------------------------------------------
def rodar_demo_drone(area_largura_m=4.0, area_comprimento_m=3.0,
                      altitude_m=12.0, altura_pilha_m=0.6):
    plano = PlanoDeVoo(area_largura_m, area_comprimento_m, altitude_m=altitude_m)
    gsd = plano.gsd_m_por_pixel()
    trajeto = plano.gerar_trajeto()
    n_fotos = plano.n_fotos_estimadas()

    print("=== Planejamento de voo ===")
    print(f"Altitude: {altitude_m} m | GSD estimado: {gsd*100:.2f} cm/pixel")
    print(f"Fotos estimadas: ~{n_fotos} | Pontos do trajeto: {len(trajeto)}")

    print("\n=== DEM baseline (área vazia) ===")
    dem_vazio, x, y = simular_dem(area_largura_m, area_comprimento_m, 0.0, gsd, semente=1)

    print("=== DEM com a pilha de fertilizante ===")
    dem_cheio, _, _ = simular_dem(area_largura_m, area_comprimento_m, altura_pilha_m, gsd, semente=2)

    volume, massa, diferenca = calcular_volume_dem(dem_vazio, dem_cheio, gsd)

    print(f"\nVolume estimado: {volume:.4f} m3")
    print(f"Massa estimada:  {massa:.1f} kg ({massa/1000:.3f} t)")

    return plano, trajeto, dem_vazio, dem_cheio, diferenca, volume, massa, x, y


def visualizar_dem_interativo(diferenca, x, y, volume_m3, massa_kg,
                               caminho_saida="saida/dem_drone_interativo.html"):
    import os
    import plotly.graph_objects as go

    os.makedirs(os.path.dirname(caminho_saida) or ".", exist_ok=True)

    fig = go.Figure(data=[go.Surface(
        z=diferenca, x=x, y=y, colorscale="YlOrBr",
        colorbar=dict(title="altura (m)"),
        hovertemplate="X: %{x:.2f} m<br>Y: %{y:.2f} m<br>altura: %{z:.3f} m<extra></extra>",
    )])
    fig.update_layout(
        title=f"DEM da pilha (fotogrametria de drone) — volume: {volume_m3:.3f} m³ · massa: {massa_kg:.0f} kg",
        scene=dict(xaxis_title="X (m)", yaxis_title="Y (m)", zaxis_title="altura (m)",
                   aspectmode="data"),
        margin=dict(l=0, r=0, t=60, b=0),
    )
    fig.write_html(caminho_saida)
    print(f"DEM interativo salvo em {caminho_saida}")


if __name__ == "__main__":
    plano, trajeto, dem_vazio, dem_cheio, diferenca, volume, massa, x, y = rodar_demo_drone()
    visualizar_dem_interativo(diferenca, x, y, volume, massa)