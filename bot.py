import os
import io
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Nama role yang boleh isi stock/harga & konfirmasi pembayaran (role kamu sendiri
# sebagai penjual). Buat role ini di server (Server Settings > Roles) lalu pasang
# ke akun kamu. Admin server otomatis dianggap Owner juga.
OWNER_ROLE_NAME = os.getenv("OWNER_ROLE_NAME", "Owner")

# Path gambar QRIS lokal (taruh file di folder assets/ sejajar dengan bot.py)
QRIS_IMAGE_PATH = "assets/qris.png"

# Nama channel tempat bot otomatis posting proof/testimoni setelah transaksi selesai
TESTIMONI_CHANNEL_NAME = os.getenv("TESTIMONI_CHANNEL_NAME", "testimoni")

# Path folder font (dipakai buat generate gambar kartu proof/testimoni)
FONT_DIR = "assets/fonts"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class OrderBot(commands.Bot):

  def __init__(self):
    super().__init__(command_prefix="!", intents=intents)

  async def setup_hook(self):
    await self.tree.sync()
    print("Slash commands berhasil disinkronkan secara global!")

  async def on_guild_join(self, guild: discord.Guild):
    self.tree.copy_global_to(guild=guild)
    await self.tree.sync(guild=guild)


bot = OrderBot()

# Simpan data order sementara di memory, keyed by channel id.
# Kalau bot restart data ini hilang -- kalau butuh persist antar-restart,
# ganti dict ini dengan baca/tulis ke file JSON atau database.
ORDER_DATA: dict[int, dict] = {}


def is_owner(member: discord.Member) -> bool:
  if member.guild_permissions.administrator:
    return True
  return any(role.name == OWNER_ROLE_NAME for role in member.roles)


def cari_channel(guild: discord.Guild, keyword: str) -> discord.TextChannel | None:
  """Cari text channel yang NAMANYA MENGANDUNG keyword (case-insensitive),
  bukan harus sama persis, biar tetep ketemu walau ada emoji di depan nama."""
  keyword = keyword.lower()
  for channel in guild.text_channels:
    if keyword in channel.name.lower():
      return channel
  return None


def cari_kategori_order(guild: discord.Guild) -> discord.CategoryChannel | None:
  """Cari kategori yang namanya MENGANDUNG 'active tickets' (case-insensitive),
  biar tetep ketemu walau format emoji/spasi di nama kategori beda-beda."""
  keyword = "active tickets"
  for category in guild.categories:
    if keyword in category.name.lower():
      return category
  return None


def buat_embed_dasar(
    guild: discord.Guild,
    title: str,
    description: str = None,
    color: discord.Color = discord.Color.blue(),
) -> discord.Embed:
  """Bikin embed dengan tampilan konsisten -- kasih thumbnail ikon server
  (kalau ada) & timestamp otomatis."""
  embed = discord.Embed(title=title, description=description, color=color)
  if guild.icon:
    embed.set_thumbnail(url=guild.icon.url)
  embed.timestamp = discord.utils.utcnow()
  return embed


def cari_owner_role(guild: discord.Guild) -> discord.Role | None:
  return discord.utils.get(guild.roles, name=OWNER_ROLE_NAME)


# ---------- Step 1: Modal awal -- nama item + jumlah yang mau dibeli ----------
class OrderItemModal(discord.ui.Modal, title="XypherStore - Order Item"):

  nama_item = discord.ui.TextInput(
      label="Nama Item / Barang",
      placeholder="Contoh: Coin / Diamond Lock",
      max_length=100,
  )
  jumlah_item = discord.ui.TextInput(
      label="Jumlah yang Ingin Dibeli (Angka saja)",
      placeholder="Contoh: 5",
      max_length=10,
  )

  async def on_submit(self, interaction: discord.Interaction):
    if not self.jumlah_item.value.strip().isdigit():
      await interaction.response.send_message(
          "❌ Jumlah item harus berupa angka.", ephemeral=True
      )
      return

    jumlah = int(self.jumlah_item.value.strip())
    if jumlah <= 0:
      await interaction.response.send_message(
          "❌ Jumlah item harus lebih dari 0.", ephemeral=True
      )
      return

    await interaction.response.defer(ephemeral=True, thinking=True)
    await create_order_channel(
        interaction, interaction.user, self.nama_item.value, jumlah
    )


# ---------- Bikin private channel order + notif ke Owner ----------
async def create_order_channel(
    interaction: discord.Interaction,
    buyer: discord.Member,
    item_name: str,
    jumlah_diminta: int,
):
  guild = interaction.guild
  channel_name = f"order-{buyer.name.lower()}"

  existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
  if existing_channel:
    await interaction.followup.send(
        f"⚠️ Kamu sudah memiliki tiket order aktif di {existing_channel.mention}!",
        ephemeral=True,
    )
    return

  owner_role = cari_owner_role(guild)

  overwrites = {
      guild.default_role: discord.PermissionOverwrite(read_messages=False),
      buyer: discord.PermissionOverwrite(read_messages=True, send_messages=True),
      guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
  }
  if owner_role:
    overwrites[owner_role] = discord.PermissionOverwrite(
        read_messages=True, send_messages=True
    )

  order_channel = await guild.create_text_channel(
      channel_name, overwrites=overwrites, category=cari_kategori_order(guild)
  )

  # Stock & harga belum diisi -- nunggu Owner input dulu lewat tombol di bawah.
  ORDER_DATA[order_channel.id] = {
      "buyer_id": buyer.id,
      "item_name": item_name,
      "jumlah_diminta": jumlah_diminta,
      "stock": None,
      "harga_per_item": None,
      "jumlah_final": None,
      "total": None,
      "stock_prompt_message_id": None,
      "buyer_confirm_message_id": None,
      "order_cancelled": False,
      "qris_message_id": None,
      "payment_confirmed": False,
      "confirmed_by_id": None,
      "confirmed_by_name": None,
      "world": None,
      "growid": None,
      "worldgrowid_prompt_message_id": None,
  }

  intro_embed = buat_embed_dasar(
      guild,
      title="🛍️ Order Baru Masuk",
      description=(
          f"Halo {buyer.mention}! Terima kasih sudah order di XypherStore.\n"
          "───────────────────────\n"
          f"📦 **Item:** {item_name}\n"
          f"🔢 **Jumlah Diminta:** {jumlah_diminta}\n\n"
          f"{owner_role.mention if owner_role else 'Owner'}, silakan cek stock & "
          "tentukan harga untuk order ini lewat tombol di bawah."
      ),
      color=discord.Color.green(),
  )
  intro_embed.set_footer(text="XypherStore Order System")
  await order_channel.send(
      content=f"{buyer.mention} {owner_role.mention if owner_role else ''}",
      embed=intro_embed,
  )

  # Tombol tutup tiket tetap ready dari awal
  await order_channel.send(view=CloseTicketView())

  stock_prompt_embed = buat_embed_dasar(
      guild,
      title="📦 Menunggu Owner Isi Stock & Harga",
      description=(
          f"Role `{OWNER_ROLE_NAME}`, silakan klik tombol di bawah untuk isi "
          "stock saat ini & harga per item untuk order ini."
      ),
      color=discord.Color.orange(),
  )
  stock_prompt_msg = await order_channel.send(
      embed=stock_prompt_embed, view=OwnerStockPriceView()
  )
  ORDER_DATA[order_channel.id]["stock_prompt_message_id"] = stock_prompt_msg.id

  await interaction.followup.send(
      f"✅ Order kamu berhasil dibuat! Cek channel: {order_channel.mention}",
      ephemeral=True,
  )


# ---------- Tombol "Isi Stock & Harga" -- KHUSUS role Owner ----------
class OwnerStockPriceView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="📦 Isi Stock & Harga (Owner)",
      style=discord.ButtonStyle.blurple,
      custom_id="order_stock_price_btn",
  )
  async def isi_stock(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_owner(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{OWNER_ROLE_NAME}` yang bisa isi stock & harga.",
          ephemeral=True,
      )
      return

    data = ORDER_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data order tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    if data.get("stock") is not None:
      await interaction.response.send_message(
          "⚠️ Stock & harga untuk order ini sudah pernah diisi.", ephemeral=True
      )
      return

    await interaction.response.send_modal(StockPriceModal())


class StockPriceModal(discord.ui.Modal, title="Isi Stock & Harga (Owner)"):

  stock_sekarang = discord.ui.TextInput(
      label="Stock Sekarang (Angka saja)",
      placeholder="Contoh: 10",
      max_length=10,
  )
  harga_per_item = discord.ui.TextInput(
      label="Harga per Item (Angka saja)",
      placeholder="Contoh: 15000",
      max_length=20,
  )

  async def on_submit(self, interaction: discord.Interaction):
    stock_raw = self.stock_sekarang.value.strip()
    harga_raw = self.harga_per_item.value.replace(".", "").replace(",", "").strip()

    if not stock_raw.isdigit() or not harga_raw.isdigit():
      await interaction.response.send_message(
          "❌ Stock & harga harus berupa angka.", ephemeral=True
      )
      return

    channel_id = interaction.channel.id
    data = ORDER_DATA.get(channel_id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data order tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return

    stock = int(stock_raw)
    harga = int(harga_raw)
    data["stock"] = stock
    data["harga_per_item"] = harga

    await interaction.response.defer()

    # Matikan tombol Isi Stock & Harga biar gak dobel-klik
    prompt_id = data.get("stock_prompt_message_id")
    if prompt_id:
      try:
        prompt_msg = await interaction.channel.fetch_message(prompt_id)
        disabled_view = OwnerStockPriceView()
        for child in disabled_view.children:
          child.disabled = True
        await prompt_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    await kirim_info_stock_ke_buyer(interaction.channel, data)


# ---------- Kirim info stock/harga/total ke Buyer + tombol Ya/Tidak ----------
async def kirim_info_stock_ke_buyer(channel: discord.TextChannel, data: dict):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  buyer_mention = buyer.mention if buyer else "Buyer"

  stock = data["stock"]
  harga = data["harga_per_item"]
  jumlah_diminta = data["jumlah_diminta"]

  if stock <= 0:
    data["jumlah_final"] = 0
    embed = buat_embed_dasar(
        guild,
        title="😔 Stock Habis",
        description=(
            f"{buyer_mention}, maaf stock **{data['item_name']}** lagi habis.\n"
            "Silakan tutup tiket ini, atau tunggu info restock dari Owner."
        ),
        color=discord.Color.red(),
    )
    await channel.send(content=buyer_mention, embed=embed)
    return

  jumlah_final = min(jumlah_diminta, stock)
  total = harga * jumlah_final
  data["jumlah_final"] = jumlah_final
  data["total"] = total

  embed = buat_embed_dasar(
      guild,
      title="🧮 Konfirmasi Order",
      color=discord.Color.blue(),
  )
  detail = (
      f"**Item:** {data['item_name']}\n"
      f"**Stock Tersedia:** {stock}\n"
      f"**Harga per Item:** Rp {harga:,}\n"
  )
  if jumlah_final < jumlah_diminta:
    detail += (
        f"**Jumlah Diminta:** {jumlah_diminta} (⚠️ stock cuma tersisa {stock})\n"
        f"**Jumlah yang Bisa Diproses:** {jumlah_final}\n"
    )
  else:
    detail += f"**Jumlah Dibeli:** {jumlah_final}\n"
  detail += f"**Total Pembayaran:** Rp {total:,}"

  embed.add_field(name="🔹 Detail", value=detail, inline=False)
  embed.set_footer(text="XypherStore Order System")

  msg = await channel.send(
      content=(
          f"{buyer_mention}, apakah tetap mau lanjut order"
          + (f" walau cuma tersedia {jumlah_final}?" if jumlah_final < jumlah_diminta else "?")
      ),
      embed=embed,
      view=BuyerConfirmView(),
  )
  data["buyer_confirm_message_id"] = msg.id


# ---------- Tombol Ya/Tidak -- KHUSUS Buyer ----------
class BuyerConfirmView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Ya, Lanjut Order",
      style=discord.ButtonStyle.green,
      custom_id="order_confirm_yes_btn",
  )
  async def lanjut(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = ORDER_DATA.get(interaction.channel.id)
    if not data or interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di order ini yang bisa konfirmasi.", ephemeral=True
      )
      return

    disabled_view = BuyerConfirmView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    await kirim_payment_embed(interaction.channel, data)

  @discord.ui.button(
      label="❌ Tidak, Batalkan",
      style=discord.ButtonStyle.red,
      custom_id="order_confirm_no_btn",
  )
  async def batal(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = ORDER_DATA.get(interaction.channel.id)
    if not data or interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di order ini yang bisa konfirmasi.", ephemeral=True
      )
      return

    data["order_cancelled"] = True
    disabled_view = BuyerConfirmView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    await interaction.channel.send(
        embed=discord.Embed(
            description="🚫 Order dibatalkan oleh Buyer. Silakan tutup tiket ini "
            "lewat tombol 'Tutup Tiket'.",
            color=discord.Color.red(),
        )
    )


# ---------- Kirim embed pembayaran + QRIS ----------
async def kirim_payment_embed(channel: discord.TextChannel, data: dict):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  buyer_mention = buyer.mention if buyer else "Buyer"

  payment_embed = buat_embed_dasar(
      guild,
      title="💳 Pembayaran ke XypherStore",
      color=discord.Color.blue(),
  )
  payment_embed.set_footer(text="XypherStore Order System")
  payment_embed.add_field(
      name="🔹 Detail Transaksi",
      value=(
          f"**Item:** {data['item_name']}\n"
          f"**Harga per Item:** Rp {data['harga_per_item']:,}\n"
          f"**Jumlah:** {data['jumlah_final']}\n"
          f"**Total Pembayaran:** Rp {data['total']:,}"
      ),
      inline=False,
  )
  payment_embed.add_field(
      name="📋 Langkah Selanjutnya",
      value=(
          f"1️⃣ {buyer_mention} transfer sesuai **Total Pembayaran** di atas "
          "lewat QRIS di bawah.\n"
          f"2️⃣ Setelah dana masuk, Owner akan konfirmasi pembayaran.\n"
          "3️⃣ Kamu akan diminta isi World & GrowID untuk pengiriman item."
      ),
      inline=False,
  )

  files = []
  if os.path.exists(QRIS_IMAGE_PATH):
    file = discord.File(QRIS_IMAGE_PATH, filename="qris.png")
    payment_embed.set_image(url="attachment://qris.png")
    files.append(file)
  else:
    payment_embed.add_field(
        name="⚠️ QRIS belum di-setup",
        value="Admin belum upload gambar QRIS.",
        inline=False,
    )

  qris_message = await channel.send(
      content=buyer_mention, embed=payment_embed, files=files, view=ConfirmPaymentView()
  )
  data["qris_message_id"] = qris_message.id


# ---------- Tombol Konfirmasi Pembayaran -- KHUSUS Owner ----------
class ConfirmPaymentView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="✅ Konfirmasi Pembayaran (Owner)",
      style=discord.ButtonStyle.blurple,
      custom_id="order_confirm_payment_btn",
  )
  async def confirm_payment(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    if not is_owner(interaction.user):
      await interaction.response.send_message(
          f"❌ Cuma role `{OWNER_ROLE_NAME}` yang bisa konfirmasi pembayaran.",
          ephemeral=True,
      )
      return

    data = ORDER_DATA.get(interaction.channel.id)
    if not data:
      await interaction.response.send_message(
          "⚠️ Data order tidak ditemukan (mungkin bot sempat restart).",
          ephemeral=True,
      )
      return
    if data.get("payment_confirmed"):
      await interaction.response.send_message(
          "⚠️ Pembayaran untuk order ini sudah dikonfirmasi sebelumnya.",
          ephemeral=True,
      )
      return

    data["payment_confirmed"] = True
    data["confirmed_by_id"] = interaction.user.id
    data["confirmed_by_name"] = interaction.user.display_name

    disabled_view = ConfirmPaymentView()
    for child in disabled_view.children:
      child.disabled = True
    await interaction.response.edit_message(view=disabled_view)

    buyer = interaction.guild.get_member(data["buyer_id"])
    buyer_mention = buyer.mention if buyer else "Buyer"

    ringkasan_embed = buat_embed_dasar(
        interaction.guild,
        title="✅ Pembayaran Terkonfirmasi",
        color=discord.Color.gold(),
    )
    ringkasan_embed.add_field(name="📦 Item", value=data["item_name"], inline=True)
    ringkasan_embed.add_field(
        name="💵 Harga per Item", value=f"Rp {data['harga_per_item']:,}", inline=True
    )
    ringkasan_embed.add_field(name="🔢 Jumlah", value=str(data["jumlah_final"]), inline=True)
    ringkasan_embed.add_field(
        name="💰 Total Dibayar", value=f"Rp {data['total']:,}", inline=False
    )
    ringkasan_embed.set_footer(
        text="Silakan isi World & GrowID untuk lanjut pengiriman item."
    )

    await interaction.channel.send(content=buyer_mention, embed=ringkasan_embed)

    prompt_msg = await interaction.channel.send(
        content=buyer_mention,
        embed=discord.Embed(
            description="🔑 Buyer, silakan klik tombol di bawah untuk isi World & "
            "GrowID kamu (akun yang bakal nerima item).",
            color=discord.Color.blue(),
        ),
        view=WorldGrowIDView(),
    )
    data["worldgrowid_prompt_message_id"] = prompt_msg.id


# ---------- Tombol "Isi World & GrowID" -- KHUSUS Buyer ----------
class WorldGrowIDView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔑 Isi World & GrowID (Buyer)",
      style=discord.ButtonStyle.green,
      custom_id="order_worldgrowid_btn",
  )
  async def isi_world_growid(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    data = ORDER_DATA.get(interaction.channel.id)
    if not data or interaction.user.id != data["buyer_id"]:
      await interaction.response.send_message(
          "❌ Cuma Buyer di order ini yang bisa isi bagian ini.", ephemeral=True
      )
      return

    if data.get("world") is not None:
      await interaction.response.send_message(
          "⚠️ World & GrowID sudah diisi sebelumnya.", ephemeral=True
      )
      return

    await interaction.response.send_modal(WorldGrowIDModal())


class WorldGrowIDModal(discord.ui.Modal, title="World & GrowID (Buyer)"):

  world = discord.ui.TextInput(
      label="Nama World Growtopia",
      placeholder="Contoh: XYPHERSTORE",
      max_length=50,
  )
  growid = discord.ui.TextInput(
      label="GrowID Penerima Item",
      placeholder="Contoh: XYPHERSTORE",
      max_length=50,
  )

  async def on_submit(self, interaction: discord.Interaction):
    channel_id = interaction.channel.id
    data = ORDER_DATA.get(channel_id, {})
    data["world"] = self.world.value
    data["growid"] = self.growid.value
    ORDER_DATA[channel_id] = data

    # Matikan tombol isi World & GrowID biar gak dobel-isi
    prompt_id = data.get("worldgrowid_prompt_message_id")
    if prompt_id:
      try:
        prompt_msg = await interaction.channel.fetch_message(prompt_id)
        disabled_view = WorldGrowIDView()
        for child in disabled_view.children:
          child.disabled = True
        await prompt_msg.edit(view=disabled_view)
      except discord.NotFound:
        pass

    owner_role = cari_owner_role(interaction.guild)
    final_embed = buat_embed_dasar(
        interaction.guild,
        title="🎉 Order Selesai -- Siap Dikirim",
        color=discord.Color.green(),
    )
    final_embed.add_field(name="📦 Item", value=data.get("item_name", "-"), inline=True)
    final_embed.add_field(name="🔢 Jumlah", value=str(data.get("jumlah_final", "-")), inline=True)
    final_embed.add_field(
        name="💰 Total", value=f"Rp {data.get('total', 0):,}", inline=True
    )
    final_embed.add_field(name="🌍 World", value=self.world.value, inline=True)
    final_embed.add_field(
        name="🧑‍🌾 GrowID Penerima", value=self.growid.value, inline=True
    )
    final_embed.set_footer(
        text=f"{OWNER_ROLE_NAME}, silakan drop item ke GrowID di atas, di world yang sudah ditentukan."
    )

    await interaction.response.send_message(
        content=owner_role.mention if owner_role else "", embed=final_embed
    )

    await kirim_testimoni(interaction.channel, data)


# ---------- Generator gambar kartu proof/testimoni (pakai Pillow) ----------
_KARTU_BG = (18, 20, 26)
_KARTU_CARD = (30, 33, 41)
_KARTU_GOLD = (245, 197, 66)
_KARTU_GREEN = (67, 181, 129)
_KARTU_TEXT = (255, 255, 255)
_KARTU_MUTED = (148, 155, 168)
_KARTU_W, _KARTU_H = 1000, 560


def _teks_aman(teks: str, fallback: str = "-") -> str:
  """Buang karakter unicode 'fancy'/emoji yang gak punya glyph di font Poppins
  (misal nickname yang pakai gaya font aneh-aneh), biar gak muncul kotak tofu
  di kartu. Huruf/angka/tanda baca biasa (termasuk aksen umum) tetap aman.
  Kalau hasil akhirnya gak ada huruf/angka sama sekali (nickname-nya fancy
  semua), pakai fallback daripada nyisain simbol doang."""
  if not teks:
    return fallback
  hasil = "".join(c for c in teks if ord(c) < 0x2000)
  hasil = " ".join(hasil.split())
  if not hasil or not any(c.isalnum() for c in hasil):
    return fallback
  return hasil


def _font(nama_file: str, size: int) -> ImageFont.FreeTypeFont:
  try:
    return ImageFont.truetype(f"{FONT_DIR}/{nama_file}", size)
  except OSError:
    # Fallback ke font default Pillow kalau file font gak ketemu di server
    return ImageFont.load_default(size=size)


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
  img = ImageOps.fit(img.convert("RGBA"), (size, size), Image.LANCZOS)
  mask = Image.new("L", (size, size), 0)
  ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
  out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
  out.paste(img, (0, 0), mask)
  return out


def _dashed_line(draw: ImageDraw.ImageDraw, x1, y, x2, color, dash=10, gap=8, width=2):
  x = x1
  while x < x2:
    draw.line([(x, y), (min(x + dash, x2), y)], fill=color, width=width)
    x += dash + gap


def _buat_kartu_proof(
    item_name: str,
    jumlah,
    total: int,
    buyer_name: str,
    owner_name: str,
    ticket_label: str,
    timestamp_str: str,
    logo_bytes: bytes = None,
    buyer_avatar_bytes: bytes = None,
    owner_avatar_bytes: bytes = None,
) -> io.BytesIO:
  """Bikin gambar kartu proof transaksi (bentuk tiket) & kembalikan sebagai
  BytesIO PNG, siap dikirim lewat discord.File."""
  item_name = _teks_aman(item_name, "-")
  buyer_name = _teks_aman(buyer_name, "Buyer")
  owner_name = _teks_aman(owner_name, "Owner")

  base = Image.new("RGB", (_KARTU_W, _KARTU_H), _KARTU_BG)
  draw = ImageDraw.Draw(base)

  card_box = (40, 40, _KARTU_W - 40, _KARTU_H - 40)
  card_layer = Image.new("RGBA", (_KARTU_W, _KARTU_H), (0, 0, 0, 0))
  ImageDraw.Draw(card_layer).rounded_rectangle(card_box, radius=28, fill=_KARTU_CARD + (255,))
  base.paste(card_layer, (0, 0), card_layer)
  draw = ImageDraw.Draw(base)

  # Notch ala tiket di sisi kiri & kanan
  notch_y = 40 + int((_KARTU_H - 80) * 0.42)
  r_notch = 22
  draw.ellipse((card_box[0] - r_notch, notch_y - r_notch, card_box[0] + r_notch, notch_y + r_notch), fill=_KARTU_BG)
  draw.ellipse((card_box[2] - r_notch, notch_y - r_notch, card_box[2] + r_notch, notch_y + r_notch), fill=_KARTU_BG)

  # Header: logo toko + judul + badge "SELESAI"
  header_y = 78
  if logo_bytes:
    try:
      logo_img = Image.open(io.BytesIO(logo_bytes))
      logo = _circle_crop(logo_img, 72)
      base.paste(logo, (74, header_y), logo)
      text_x = 74 + 72 + 22
    except Exception:
      text_x = 74
  else:
    text_x = 74

  f_brand = _font("Poppins-Bold.ttf", 30)
  f_sub = _font("Poppins-Regular.ttf", 17)
  draw.text((text_x, header_y + 4), "XypherStore", font=f_brand, fill=_KARTU_TEXT)
  draw.text((text_x, header_y + 42), "Bukti Transaksi Order", font=f_sub, fill=_KARTU_MUTED)

  f_badge = _font("Poppins-SemiBold.ttf", 18)
  badge_text = "SELESAI"
  icon_d = 20
  text_w = draw.textlength(badge_text, font=f_badge)
  badge_w = 20 + icon_d + 10 + text_w + 20
  badge_h = 42
  badge_box = (_KARTU_W - 40 - badge_w - 20, header_y + 8, _KARTU_W - 40 - 20, header_y + 8 + badge_h)
  draw.rounded_rectangle(badge_box, radius=badge_h // 2, fill=_KARTU_GREEN)
  icon_cx = badge_box[0] + 20 + icon_d / 2
  icon_cy = (badge_box[1] + badge_box[3]) / 2
  check_color = (15, 20, 15)
  draw.line(
      [(icon_cx - 6, icon_cy), (icon_cx - 2, icon_cy + 5), (icon_cx + 7, icon_cy - 6)],
      fill=check_color, width=3, joint="curve",
  )
  draw.text((icon_cx + icon_d / 2 + 10, badge_box[1] + 9), badge_text, font=f_badge, fill=check_color)

  # Garis putus-putus pemisah header
  _dashed_line(draw, card_box[0] + 46, notch_y, card_box[2] - 46, (60, 64, 74))

  # Body: Item / Jumlah / Total
  body_y = notch_y + 42
  f_label = _font("Poppins-Regular.ttf", 16)
  f_value = _font("Poppins-SemiBold.ttf", 26)
  col_w = (card_box[2] - card_box[0] - 92) / 3
  for i, (label, value, color) in enumerate([
      ("ITEM", item_name, _KARTU_TEXT),
      ("JUMLAH", str(jumlah), _KARTU_TEXT),
      ("TOTAL", f"Rp {total:,}", _KARTU_GOLD),
  ]):
    x = card_box[0] + 46 + i * col_w
    draw.text((x, body_y), label, font=f_label, fill=_KARTU_MUTED)
    draw.text((x, body_y + 26), value, font=f_value, fill=color)

  # Garis putus-putus sebelum footer
  sep2_y = body_y + 100
  _dashed_line(draw, card_box[0] + 46, sep2_y, card_box[2] - 46, (60, 64, 74))

  # Buyer & Owner (avatar + nama)
  row_y = sep2_y + 30
  avatar_size = 52
  f_role = _font("Poppins-Regular.ttf", 14)
  f_name = _font("Poppins-SemiBold.ttf", 18)
  half_col = (card_box[2] - card_box[0] - 92) / 2

  def _draw_person(x, role, name, avatar_bytes):
    name_x = x
    if avatar_bytes:
      try:
        av_img = Image.open(io.BytesIO(avatar_bytes))
        av = _circle_crop(av_img, avatar_size)
        base.paste(av, (int(x), row_y), av)
        name_x = x + avatar_size + 14
      except Exception:
        pass
    draw.text((name_x, row_y + 2), role, font=f_role, fill=_KARTU_MUTED)
    draw.text((name_x, row_y + 20), name, font=f_name, fill=_KARTU_TEXT)

  _draw_person(card_box[0] + 46, "BUYER", buyer_name, buyer_avatar_bytes)
  _draw_person(card_box[0] + 46 + half_col, "OWNER", owner_name, owner_avatar_bytes)

  # Footer
  f_footer = _font("Poppins-Regular.ttf", 14)
  draw.text(
      (card_box[0] + 46, card_box[3] - 40),
      f"{ticket_label}  •  {timestamp_str}",
      font=f_footer,
      fill=_KARTU_MUTED,
  )

  buffer = io.BytesIO()
  base.save(buffer, format="PNG")
  buffer.seek(0)
  return buffer


async def _baca_avatar_bytes(member: discord.Member | None) -> bytes | None:
  if member is None:
    return None
  try:
    return await member.display_avatar.read()
  except Exception:
    return None


async def kirim_testimoni(channel: discord.TextChannel, data: dict):
  guild = channel.guild
  buyer = guild.get_member(data["buyer_id"])
  owner_member = None
  if data.get("confirmed_by_id"):
    owner_member = guild.get_member(data["confirmed_by_id"])

  logo_bytes = None
  if guild.icon:
    try:
      logo_bytes = await guild.icon.read()
    except Exception:
      logo_bytes = None

  buyer_avatar_bytes = await _baca_avatar_bytes(buyer)
  owner_avatar_bytes = await _baca_avatar_bytes(owner_member)

  waktu_sekarang = discord.utils.utcnow().strftime("%d %b %Y, %H:%M UTC")

  buffer = _buat_kartu_proof(
      item_name=data.get("item_name", "-"),
      jumlah=data.get("jumlah_final", "-"),
      total=data.get("total", 0),
      buyer_name=buyer.display_name if buyer else "Buyer",
      owner_name=data.get("confirmed_by_name", "Owner"),
      ticket_label=f"Ticket: #{channel.name}",
      timestamp_str=waktu_sekarang,
      logo_bytes=logo_bytes,
      buyer_avatar_bytes=buyer_avatar_bytes,
      owner_avatar_bytes=owner_avatar_bytes,
  )
  proof_file = discord.File(buffer, filename="proof_transaksi.png")

  testimoni_channel = cari_channel(guild, TESTIMONI_CHANNEL_NAME)
  target_channel = testimoni_channel if testimoni_channel else channel
  if not testimoni_channel:
    await channel.send(
        f"⚠️ Channel `#{TESTIMONI_CHANNEL_NAME}` belum ada, proof dikirim di sini dulu:"
    )
  await target_channel.send(file=proof_file)

  testimoni_channel = cari_channel(guild, TESTIMONI_CHANNEL_NAME)
  if testimoni_channel:
    await testimoni_channel.send(embed=testimoni_embed)
  else:
    await channel.send(
        content=f"⚠️ Channel `#{TESTIMONI_CHANNEL_NAME}` belum ada, testimoni dikirim di sini dulu:",
        embed=testimoni_embed,
    )


# ---------- Tombol Tutup Tiket ----------
class CloseTicketView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🔒 Tutup Tiket",
      style=discord.ButtonStyle.red,
      custom_id="order_close_ticket_btn",
  )
  async def close_ticket(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_message(
        "⚠️ Tiket akan ditutup dan channel ini akan dihapus dalam 3 detik...",
        ephemeral=False,
    )
    ORDER_DATA.pop(interaction.channel.id, None)
    await asyncio.sleep(3)
    await interaction.channel.delete()


# ---------- Tombol Utama "Order Item" ----------
class OrderButtonView(discord.ui.View):

  def __init__(self):
    super().__init__(timeout=None)

  @discord.ui.button(
      label="🛒 Order Item",
      style=discord.ButtonStyle.green,
      custom_id="order_create_btn",
  )
  async def order_item(
      self, interaction: discord.Interaction, button: discord.ui.Button
  ):
    await interaction.response.send_modal(OrderItemModal())


@bot.event
async def on_ready():
  print(f"Bot {bot.user.name} berhasil online dan siap bertugas!")
  bot.add_view(OrderButtonView())
  bot.add_view(CloseTicketView())
  bot.add_view(OwnerStockPriceView())
  bot.add_view(BuyerConfirmView())
  bot.add_view(ConfirmPaymentView())
  bot.add_view(WorldGrowIDView())


@bot.command()
@commands.has_permissions(administrator=True)
async def setup_order(ctx):
  embed = discord.Embed(
      title="🛍️ XypherStore Order System",
      description=(
          "Mau order item?\nKlik tombol di bawah untuk membuat **Private Channel** "
          "order secara otomatis!"
      ),
      color=discord.Color.blue(),
  )
  embed.set_footer(text="XypherStore Automated System")
  await ctx.send(embed=embed, view=OrderButtonView())
  await ctx.message.delete()


bot.run(TOKEN)