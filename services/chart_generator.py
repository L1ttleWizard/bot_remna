import io
import time
from datetime import datetime
import matplotlib
# Use Agg backend for headless environments
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

def generate_node_load_chart(node_name: str, metrics_history: list) -> bytes:
    """
    Renders a premium, dark-themed, high-resolution chart of node metrics.
    
    metrics_history is a list of tuples/lists: (timestamp, cpu_load, ram_usage, users_online)
    Returns: Bytes of the generated PNG image.
    """
    # 1. Prepare data
    if not metrics_history:
        # Create an empty/placeholder chart
        fig, ax = plt.subplots(figsize=(10, 5), facecolor='#121214')
        ax.set_facecolor('#121214')
        ax.text(0.5, 0.5, "Нет данных за последние 24 часа", 
                color='#8E8E93', fontsize=14, ha='center', va='center')
        ax.set_title(f"Нагрузка сервера {node_name}", color='#FFFFFF', fontsize=14, pad=15)
        ax.axis('off')
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150, bbox_inches='tight', facecolor='#121214')
        buf.seek(0)
        plt.close(fig)
        return buf.getvalue()

    timestamps = [row[0] for row in metrics_history]
    cpu_loads = [row[1] for row in metrics_history]
    ram_usages = [row[2] for row in metrics_history]
    users = [row[3] for row in metrics_history]

    # Convert timestamps to datetime objects
    dates = [datetime.fromtimestamp(ts) for ts in timestamps]

    # 2. Style configuration (Dark Cyberpunk / Glassmorphism theme)
    plt.style.use('dark_background')
    
    # Subplots: 
    # Top: CPU & RAM (Percentage 0-100)
    # Bottom: Users Online
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, 
                                   facecolor='#121214', 
                                   gridspec_kw={'height_ratios': [2, 1]})
    
    # Configure axes background
    ax1.set_facecolor('#18181C')
    ax2.set_facecolor('#18181C')

    # Plot CPU and RAM on top subplot
    ax1.plot(dates, cpu_loads, label='CPU Load (%)', color='#00F0FF', linewidth=2.0, alpha=0.9)
    ax1.plot(dates, ram_usages, label='RAM Usage (%)', color='#FF007F', linewidth=2.0, alpha=0.9)
    
    # Fill under the curves for premium gradient look
    ax1.fill_between(dates, cpu_loads, color='#00F0FF', alpha=0.1)
    ax1.fill_between(dates, ram_usages, color='#FF007F', alpha=0.08)

    ax1.set_ylabel('Загрузка (%)', color='#E5E5EA', fontsize=10, labelpad=10)
    ax1.set_ylim(-2, 102)
    ax1.grid(True, color='#2C2C30', linestyle='--', linewidth=0.5)
    
    # Add a legend
    legend1 = ax1.legend(loc='upper left', frameon=True, facecolor='#18181C', edgecolor='#2C2C30')
    for text in legend1.get_texts():
        text.set_color('#E5E5EA')

    # Plot Users Online on bottom subplot
    ax2.plot(dates, users, label='Онлайн-пользователи', color='#39FF14', linewidth=2.0, alpha=0.9)
    ax2.fill_between(dates, users, color='#39FF14', alpha=0.12)
    
    ax2.set_ylabel('Пользователи', color='#E5E5EA', fontsize=10, labelpad=10)
    
    # Handle user count y-axis to be nice integers
    max_users = max(users) if users else 0
    ax2.set_ylim(-0.2, max(3, max_users * 1.15))
    ax2.grid(True, color='#2C2C30', linestyle='--', linewidth=0.5)
    
    legend2 = ax2.legend(loc='upper left', frameon=True, facecolor='#18181C', edgecolor='#2C2C30')
    for text in legend2.get_texts():
        text.set_color('#E5E5EA')

    # Title & Formatting
    fig.suptitle(f"График нагрузки сервера: {node_name}", color='#FFFFFF', fontsize=14, weight='bold', y=0.96)
    
    # Format x-axis timestamps (HH:MM)
    time_diff = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0
    if time_diff > 86400:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        formatter = mdates.DateFormatter('%d.%m %H:%M')
    else:
        locator = mdates.HourLocator(interval=2 if time_diff > 43200 else 1)
        formatter = mdates.DateFormatter('%H:%M')

    ax2.xaxis.set_major_locator(locator)
    ax2.xaxis.set_major_formatter(formatter)
    
    # Rotate x labels for readability
    plt.setp(ax2.get_xticklabels(), rotation=30, ha='right', color='#8E8E93', fontsize=9)
    plt.setp(ax1.get_yticklabels(), color='#8E8E93', fontsize=9)
    plt.setp(ax2.get_yticklabels(), color='#8E8E93', fontsize=9)

    # Clean borders (spines)
    for ax in (ax1, ax2):
        for spine in ('top', 'bottom', 'left', 'right'):
            ax.spines[spine].set_color('#2C2C30')
            ax.spines[spine].set_linewidth(0.8)

    # Adjust layout to fit everything nicely
    plt.tight_layout(rect=[0, 0.03, 1, 0.93])

    # Save to buffer
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#121214')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


def _format_bytes_y_axis(x, pos):
    if x >= 1024**4:
        return f"{x/(1024**4):.1f} ТБ"
    if x >= 1024**3:
        return f"{x/(1024**3):.1f} ГБ"
    if x >= 1024**2:
        return f"{x/(1024**2):.1f} МБ"
    if x >= 1024:
        return f"{x/1024:.1f} КБ"
    return f"{int(x)} Б"


def generate_total_traffic_chart(categories: list[str], sparkline_data: list[int]) -> bytes:
    """
    Генерирует график общего суточного трафика нод за указанные даты в темном киберпанк стиле.
    """
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#121214')
    ax.set_facecolor('#18181C')

    # Convert categories (YYYY-MM-DD) to datetimes
    dates = [datetime.strptime(cat, "%Y-%m-%d") for cat in categories]
    traffic_vals = [int(v) for v in sparkline_data]

    # Plot total traffic
    ax.plot(dates, traffic_vals, label='Общий трафик нод', color='#00F0FF', linewidth=2.5, alpha=0.95)
    ax.fill_between(dates, traffic_vals, color='#00F0FF', alpha=0.1)

    # Title & Labels
    ax.set_title("График общего трафика серверов за период", color='#FFFFFF', fontsize=14, weight='bold', pad=15)
    ax.set_ylabel("Потребление трафика", color='#E5E5EA', fontsize=10, labelpad=10)
    ax.grid(True, color='#2C2C30', linestyle='--', linewidth=0.5)

    # Format Axes
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    formatter = mdates.DateFormatter('%d.%m')
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    
    # Custom format for Y axis bytes
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_format_bytes_y_axis))

    plt.setp(ax.get_xticklabels(), rotation=30, ha='right', color='#8E8E93', fontsize=9)
    plt.setp(ax.get_yticklabels(), color='#8E8E93', fontsize=9)

    # Legend
    legend = ax.legend(loc='upper left', frameon=True, facecolor='#18181C', edgecolor='#2C2C30')
    for text in legend.get_texts():
        text.set_color('#E5E5EA')

    # Clean borders
    for spine in ('top', 'bottom', 'left', 'right'):
        ax.spines[spine].set_color('#2C2C30')
        ax.spines[spine].set_linewidth(0.8)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#121214')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()


def generate_nodes_traffic_comparison_chart(categories: list[str], series: list[dict]) -> bytes:
    """
    Генерирует график сравнения трафика по нодам (line chart) в темном неоновом стиле с контрастными цветами.
    """
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(10, 5), facecolor='#121214')
    ax.set_facecolor('#18181C')

    dates = [datetime.strptime(cat, "%Y-%m-%d") for cat in categories]
    
    # Сортируем серии по суммарному трафику (чтобы легенда была упорядоченной)
    sorted_series = sorted(series, key=lambda s: sum(int(x) for x in s.get('data') or []), reverse=True)

    # Высококонтрастная неоновая палитра
    contrast_palette = [
        '#00F0FF',  # Неоновый Голубой (Cyan)
        '#FF007F',  # Неоновый Розовый (Magenta)
        '#39FF14',  # Неоновый Зеленый (Lime)
        '#FFCC00',  # Неоновый Желтый
        '#FF5E00',  # Яркий Оранжевый
        '#BF00FF',  # Яркий Фиолетовый
        '#FF0000',  # Ярко-красный
        '#FFFFFF',  # Белый
    ]

    for idx, s in enumerate(sorted_series):
        data = [int(v) for v in (s.get('data') or [])]
        # Выравниваем длину данных по оси X
        if len(data) < len(dates):
            data += [0] * (len(dates) - len(data))
        data = data[:len(dates)]
        
        label = s.get('name') or f"Нода #{idx+1}"
        color = contrast_palette[idx % len(contrast_palette)]
        
        # Строим линию для каждой ноды без заливки
        ax.plot(dates, data, label=label, color=color, linewidth=2.0, alpha=0.95)

    # Title & Labels
    ax.set_title("Сравнение распределения трафика по серверам", color='#FFFFFF', fontsize=14, weight='bold', pad=15)
    ax.set_ylabel("Потребление трафика", color='#E5E5EA', fontsize=10, labelpad=10)
    ax.grid(True, color='#2C2C30', linestyle='--', linewidth=0.5)

    # Format Axes
    locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
    formatter = mdates.DateFormatter('%d.%m')
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(formatter)
    
    # Custom format for Y axis bytes
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_format_bytes_y_axis))

    plt.setp(ax.get_xticklabels(), rotation=30, ha='right', color='#8E8E93', fontsize=9)
    plt.setp(ax.get_yticklabels(), color='#8E8E93', fontsize=9)

    # Legend
    legend = ax.legend(loc='upper left', frameon=True, facecolor='#18181C', edgecolor='#2C2C30')
    for text in legend.get_texts():
        text.set_color('#E5E5EA')

    # Clean borders
    for spine in ('top', 'bottom', 'left', 'right'):
        ax.spines[spine].set_color('#2C2C30')
        ax.spines[spine].set_linewidth(0.8)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150, facecolor='#121214')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

