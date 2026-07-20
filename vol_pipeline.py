import numpy as np
from scipy.interpolate import griddata
from scipy.ndimage import median_filter
from scipy.spatial import cKDTree

# ----------------------------------------------------------------------
# CONFIGURAÇÃO DO BOX
# ----------------------------------------------------------------------
BOX_LARGURA_M = 5.50
BOX_COMPRIMENTO_M = 5.00
RESOLUCAO_GRID = 200
DENSIDADE_FERTILIZANTE = 900  # kg/m3


# ----------------------------------------------------------------------
# PASSO 1 e 2: CAPTURA
# A nuvem de pontos é só um np.ndarray de shape (N, 3) - sem
# nenhuma classe wrapper.
# ----------------------------------------------------------------------
def capturar_nuvem_realsense(segundos=1):
    """Retorna um array (N, 3) de pontos XYZ vindos da RealSense."""
    import pyrealsense2 as rs

    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(rs.stream.depth, 640, 480, rs.format.z16, 30)
    pipeline.start(config)

    pc = rs.pointcloud()
    try:
        for _ in range(segundos * 30):
            frames = pipeline.wait_for_frames()
            depth_frame = frames.get_depth_frame()
        points = pc.calculate(depth_frame)
        v = np.asanyarray(points.get_vertices()).view(np.float32).reshape(-1, 3)
    finally:
        pipeline.stop()

    return v.astype(np.float64)


def gerar_nuvem_sintetica(altura_pico_m=0.0, ruido=0.003, semente=None):
    """Gera pontos simulados de uma pilha em formato de monte, para testar
    o pipeline sem hardware."""
    rng = np.random.default_rng(semente)
    n_pontos = 20000
    x = rng.uniform(0, BOX_LARGURA_M, n_pontos)
    y = rng.uniform(0, BOX_COMPRIMENTO_M, n_pontos)

    if altura_pico_m > 0:
        cx, cy = BOX_LARGURA_M / 2, BOX_COMPRIMENTO_M / 2
        raio = min(BOX_LARGURA_M, BOX_COMPRIMENTO_M) / 2.2
        dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        z = altura_pico_m * np.clip(1 - dist / raio, 0, None)
    else:
        z = np.zeros(n_pontos)

    z += rng.normal(0, ruido, n_pontos)
    return np.column_stack([x, y, z])


# ----------------------------------------------------------------------
# PASSO 4: TRATAMENTO DE RUÍDO
# ----------------------------------------------------------------------
def remover_ruido(pontos, vizinhos=20, desvio_padrao=2.0):
    """
    Para cada ponto,
    calcula a distância média até seus k vizinhos mais próximos; remove
    pontos cuja distância média foge muito do padrão da nuvem toda
    (média + desvio_padrao * desvio).
    """
    arvore = cKDTree(pontos)
    distancias, _ = arvore.query(pontos, k=vizinhos + 1)  # +1 pq inclui o próprio ponto
    dist_media = distancias[:, 1:].mean(axis=1)  # ignora distância 0 (ele mesmo)

    media_global = dist_media.mean()
    desvio_global = dist_media.std()
    limite = media_global + desvio_padrao * desvio_global

    mascara_validos = dist_media < limite
    return pontos[mascara_validos]


def nuvem_para_heightmap(pontos, resolucao=RESOLUCAO_GRID):
    x, y, z = pontos[:, 0], pontos[:, 1], pontos[:, 2]

    grid_x, grid_y = np.mgrid[
        0:BOX_LARGURA_M:complex(resolucao),
        0:BOX_COMPRIMENTO_M:complex(resolucao),
    ]

    heightmap = griddata((x, y), z, (grid_x, grid_y), method="linear")
    mascara_buraco = np.isnan(heightmap)
    if mascara_buraco.any():
        preenchido = griddata((x, y), z, (grid_x, grid_y), method="nearest")
        heightmap[mascara_buraco] = preenchido[mascara_buraco]

    heightmap = median_filter(heightmap, size=3)
    return heightmap


# ----------------------------------------------------------------------
# PASSO 3: CÁLCULO DO VOLUME
# ----------------------------------------------------------------------
def calcular_volume(heightmap_vazio, heightmap_carregado, resolucao=RESOLUCAO_GRID):
    diferenca = heightmap_carregado - heightmap_vazio
    diferenca = np.clip(diferenca, 0, None)

    area_celula = (BOX_LARGURA_M / resolucao) * (BOX_COMPRIMENTO_M / resolucao)
    volume_m3 = np.sum(diferenca) * area_celula
    return volume_m3, diferenca


def volume_para_massa(volume_m3, densidade_kg_m3=DENSIDADE_FERTILIZANTE):
    return volume_m3 * densidade_kg_m3


# ----------------------------------------------------------------------
# PASSO 5: DEMO
# ----------------------------------------------------------------------
def rodar_demo(altura_simulada_m=0.35):
    print("=== Captura de baseline (box vazio) ===")
    pontos_vazio = gerar_nuvem_sintetica(altura_pico_m=0.0, semente=1)
    pontos_vazio = remover_ruido(pontos_vazio)
    heightmap_vazio = nuvem_para_heightmap(pontos_vazio)

    print("=== Captura com carga ===")
    pontos_cheio = gerar_nuvem_sintetica(altura_pico_m=altura_simulada_m, semente=2)
    pontos_cheio = remover_ruido(pontos_cheio)
    heightmap_cheio = nuvem_para_heightmap(pontos_cheio)

    volume, mapa_diferenca = calcular_volume(heightmap_vazio, heightmap_cheio)
    massa_kg = volume_para_massa(volume)

    print(f"\nVolume estimado: {volume:.4f} m3")
    print(f"Massa estimada:  {massa_kg:.1f} kg  ({massa_kg/1000:.3f} t)")

    return heightmap_vazio, heightmap_cheio, mapa_diferenca, volume, massa_kg


def visualizar_resultado(mapa_diferenca, caminho_saida="saida/reconstrucao_pilha.png"):
    import os
    import matplotlib.pyplot as plt

    os.makedirs(os.path.dirname(caminho_saida) or ".", exist_ok=True)

    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(111, projection="3d")
    x = np.linspace(0, BOX_LARGURA_M, mapa_diferenca.shape[0])
    y = np.linspace(0, BOX_COMPRIMENTO_M, mapa_diferenca.shape[1])
    X, Y = np.meshgrid(x, y, indexing="ij")
    ax.plot_surface(X, Y, mapa_diferenca, cmap="YlOrBr", linewidth=0, antialiased=True)
    ax.set_xlabel("Largura (m)")
    ax.set_ylabel("Comprimento (m)")
    ax.set_zlabel("Altura do material (m)")
    ax.set_title("Reconstrução 3D da pilha de fertilizante (sem Open3D)")
    plt.tight_layout()
    plt.savefig(caminho_saida, dpi=150)
    print(f"Gráfico salvo em {caminho_saida}")


def visualizar_interativo(mapa_diferenca, volume_m3, massa_kg,
                           caminho_saida="saida/pilha_interativa.html"):
    """
    Gera um HTML autônomo com a pilha em 3D rotacionável (arraste o mouse),
    com zoom e leitura de altura ao passar o cursor.
    """
    import os
    import plotly.graph_objects as go

    os.makedirs(os.path.dirname(caminho_saida) or ".", exist_ok=True)

    x = np.linspace(0, BOX_LARGURA_M, mapa_diferenca.shape[0])
    y = np.linspace(0, BOX_COMPRIMENTO_M, mapa_diferenca.shape[1])

    fig = go.Figure(data=[go.Surface(
        z=mapa_diferenca,
        x=x,
        y=y,
        colorscale="YlOrBr",
        colorbar=dict(title="altura (m)"),
        hovertemplate="largura: %{x:.2f} m<br>comprimento: %{y:.2f} m<br>altura: %{z:.3f} m<extra></extra>",
    )])

    fig.update_layout(
        title=f"Pilha reconstruída — volume: {volume_m3:.3f} m³ · massa: {massa_kg:.0f} kg",
        scene=dict(
            xaxis_title="largura (m)",
            yaxis_title="comprimento (m)",
            zaxis_title="altura (m)",
            aspectmode="manual",
            aspectratio=dict(x=BOX_LARGURA_M, y=BOX_COMPRIMENTO_M, z=0.6),
        ),
        margin=dict(l=0, r=0, t=60, b=0),
    )

    fig.write_html(caminho_saida)
    print(f"Visualização interativa salva em {caminho_saida} — abra no navegador")


if __name__ == "__main__":
    _, _, mapa_diferenca, volume, massa = rodar_demo(altura_simulada_m=0.35)
    visualizar_resultado(mapa_diferenca)
    visualizar_interativo(mapa_diferenca, volume, massa)

# ----------------------------------------------------------------------
# Observação sobre pyrealsense2:
#
# Na data de hoje, o pacote pyrealsense2 também costuma atrasar o
# lançamento de wheels para versões novas do Python (mesmo padrão do
# Open3D). Se pip install pyrealsense2 falhar no seu 3.13:
#   1. verifique a página de releases do pacote no PyPI para confirmar
#      se já existe wheel pra sua versão/plataforma;
#   2. se não existir, a saída mais rápida pro hackathon é criar um
#      ambiente virtual só para a captura com Python 3.11/3.12 (via
#      pyenv ou conda), rodar a captura nele, e salvar a nuvem de pontos
#      num arquivo (.npy ou .csv) que o resto do pipeline lê normalmente
#      -- assim só o script de captura fica preso à versão antiga do
#      Python, e o processamento (que é a parte pesada) roda livre no
#      3.13.
# ----------------------------------------------------------------------