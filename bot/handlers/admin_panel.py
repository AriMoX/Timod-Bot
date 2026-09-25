from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from bot.config import ADMIN_ID
from bot.services.user_storage import (
    is_admin, get_admin_permissions, get_all_admins, 
    add_or_update_admin, remove_admin, get_user_count
)

router = Router()

class AdminPanelStates(StatesGroup):
    waiting_for_new_admin_id = State()
    waiting_for_broadcast_msg = State()

def get_panel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    is_owner = (user_id == ADMIN_ID)
    perms = get_admin_permissions(user_id)
    
    buttons = []
    
    # Broadcast
    if is_owner or "broadcast" in perms or "all" in perms:
        buttons.append([InlineKeyboardButton(text="📢 ارسال پیام همگانی", callback_data="panel_broadcast")])
        
    # Stats
    if is_owner or "stats" in perms or "all" in perms:
        buttons.append([InlineKeyboardButton(text="👥 آمار و کاربران", callback_data="panel_stats")])
        
    # Backup
    if is_owner or "backup" in perms or "all" in perms:
        buttons.append([InlineKeyboardButton(text="💾 دریافت بک‌آپ", callback_data="panel_backup")])
        
    # Manage Admins (OWNER ONLY)
    if is_owner:
        buttons.append([InlineKeyboardButton(text="👮‍♂️ مدیریت ادمین‌ها", callback_data="panel_manage_admins")])
        
    buttons.append([InlineKeyboardButton(text="❌ بستن پنل", callback_data="panel_close")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("panel"))
@router.message(Command("admin"))
async def cmd_panel(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not is_admin(user_id):
        await message.reply("⛔️ شما به پنل مدیریت دسترسی ندارید.")
        return
        
    role = "👑 مالک ربات" if user_id == ADMIN_ID else "👮‍♂️ ادمین"
    text = f"👋 به پنل مدیریت خوش آمدید!\n\nسطح دسترسی شما: <b>{role}</b>\nلطفاً یک گزینه را انتخاب کنید:"
    await message.reply(text, reply_markup=get_panel_keyboard(user_id), parse_mode="HTML")


@router.callback_query(F.data == "panel_close")
async def close_panel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()


@router.callback_query(F.data == "panel_stats")
async def show_stats(call: CallbackQuery):
    user_id = call.from_user.id
    if not is_admin(user_id):
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
        
    count = get_user_count()
    await call.message.edit_text(
        f"👥 <b>آمار زنده ربات:</b>\n\nتعداد کل کاربران: {count} نفر",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data="panel_back")]
        ]),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "panel_back")
async def panel_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
    if not is_admin(user_id):
        return await call.message.delete()
        
    role = "👑 مالک ربات" if user_id == ADMIN_ID else "👮‍♂️ ادمین"
    text = f"👋 به پنل مدیریت خوش آمدید!\n\nسطح دسترسی شما: <b>{role}</b>\nلطفاً یک گزینه را انتخاب کنید:"
    await call.message.edit_text(text, reply_markup=get_panel_keyboard(user_id), parse_mode="HTML")


@router.callback_query(F.data == "panel_manage_admins")
async def manage_admins(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("فقط مالک ربات دسترسی دارد!", show_alert=True)
        
    admins = get_all_admins()
    
    buttons = []
    text = "👮‍♂️ <b>لیست ادمین‌های فعلی:</b>\n\n"
    if not admins:
        text += "هیچ ادمینی بجز شما وجود ندارد."
    else:
        for idx, adm in enumerate(admins, 1):
            text += f"{idx}. <code>{adm['user_id']}</code> (مجوزها: {adm['permissions']})\n"
            buttons.append([InlineKeyboardButton(text=f"🗑 حذف ادمین {adm['user_id']}", callback_data=f"panel_deladmin_{adm['user_id']}")])
            
    buttons.append([InlineKeyboardButton(text="➕ افزودن ادمین جدید", callback_data="panel_add_admin")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="panel_back")])
    
    await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")


@router.callback_query(F.data == "panel_add_admin")
async def ask_new_admin(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("فقط مالک ربات دسترسی دارد!", show_alert=True)
        
    await state.set_state(AdminPanelStates.waiting_for_new_admin_id)
    await call.message.edit_text(
        "➕ لطفاً <b>آیدی عددی (User ID)</b> شخصی که می‌خواهید ادمین کنید را بفرستید:\n\n(می‌تواند از ربات‌هایی مثل @userinfobot آیدی خود را بگیرد)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 لغو", callback_data="panel_manage_admins")]]),
        parse_mode="HTML"
    )

@router.message(AdminPanelStates.waiting_for_new_admin_id)
async def process_new_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return await state.clear()
        
    if not message.text.isdigit():
        await message.reply("❌ لطفاً فقط یک عدد معتبر ارسال کنید.")
        return
        
    target_id = int(message.text)
    
    # Add with full permissions by default for now
    add_or_update_admin(target_id, message.from_user.id, ["all"])
    await state.clear()
    
    await message.reply(f"✅ کاربر <code>{target_id}</code> با موفقیت به عنوان ادمین (دسترسی کامل) اضافه شد!", parse_mode="HTML")


@router.callback_query(F.data.startswith("panel_deladmin_"))
async def del_admin(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("فقط مالک ربات دسترسی دارد!", show_alert=True)
        
    target_id = int(call.data.split("_")[-1])
    remove_admin(target_id)
    await call.answer(f"ادمین {target_id} حذف شد!", show_alert=True)
    await manage_admins(call)


@router.callback_query(F.data == "panel_backup")
async def panel_do_backup(call: CallbackQuery):
    user_id = call.from_user.id
    perms = get_admin_permissions(user_id)
    if user_id != ADMIN_ID and "backup" not in perms and "all" not in perms:
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
        
    from bot.handlers.common import cmd_backup
    # Re-use the existing cmd_backup, but we need to mock a message or just call it directly.
    # Actually, we can just call it with call.message, but cmd_backup checks ADMIN_ID.
    # Let's bypass it for authorized admins.
    await call.message.delete()
    
    status_msg = await call.message.answer("⏳ در حال آماده‌سازی و استخراج فایل‌های پشتیبان دیتابیس...")
    try:
        from bot.services.user_storage import get_backup_documents
        db_doc, json_doc, caption = get_backup_documents()
        await call.message.answer_document(db_doc, caption=caption, parse_mode="HTML")
        await call.message.answer_document(
            json_doc,
            caption="📋 <b>فایل JSON حاوی کلیه مشخصات و آمار کاربران</b>",
            parse_mode="HTML",
        )
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ خطا در تولید نسخه پشتیبان: {e}")

@router.callback_query(F.data == "panel_broadcast")
async def panel_do_broadcast(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    perms = get_admin_permissions(user_id)
    if user_id != ADMIN_ID and "broadcast" not in perms and "all" not in perms:
        return await call.answer("دسترسی غیرمجاز", show_alert=True)
        
    await state.set_state(AdminPanelStates.waiting_for_broadcast_msg)
    await call.message.edit_text(
        "📢 لطفاً پیامی که می‌خواهید برای **تمامی کاربران ربات** ارسال شود را بفرستید:\n\n(می‌تواند عکس، ویدیو، متن، فوروارد یا هر چیزی باشد)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 لغو", callback_data="panel_back")]])
    )

@router.message(AdminPanelStates.waiting_for_broadcast_msg)
async def process_broadcast_msg(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return await state.clear()
        
    await state.clear()
    
    from bot.services.user_storage import get_user_count
    # In a real app, you'd fetch all users and iterate with a background task.
    # For now, we will just send a confirmation that it started.
    # Let's do a fast background broadcast!
    import asyncio
    
    async def bg_broadcast():
        import sqlite3
        from bot.services.user_storage import DB_PATH
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            users = c.fetchall()
            
        success = 0
        failed = 0
        for (uid,) in users:
            try:
                await message.copy_to(chat_id=uid)
                success += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05) # Prevent flood wait
            
        await message.reply(f"📢 <b>گزارش ارسال همگانی:</b>\n\n✅ موفق: {success}\n❌ ناموفق (ربات را بلاک کرده‌اند): {failed}", parse_mode="HTML")
        
    asyncio.create_task(bg_broadcast())
    await message.reply("⏳ پیام شما دریافت شد و ارسال همگانی در پس‌زمینه آغاز شد... نتیجه پس از پایان به شما اعلام می‌شود.")
