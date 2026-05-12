import os
import re
import threading
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler

import imaplib2
import email
import logging
import time
from threading import Event
import asyncio
from email.header import decode_header, make_header
import pytz
from telegram.ext import ApplicationBuilder
from bs4 import BeautifulSoup
import html
import chardet

from tools.extract_verification_code import extract_verification_code


# 配置日志
log_directory = "./log"
os.makedirs(log_directory, exist_ok=True)
log_file_path = os.path.join(log_directory, "email_checker.log")

# 从环境变量读取日志配置，设置默认值
max_log_file_size = int(os.getenv('LOG_MAX_SIZE', 5 * 1024 * 1024))  # 默认 5 MB
backup_count = int(os.getenv('LOG_MAX_FILE', 5))  # 默认保留 5 个旧日志文件

logging_enabled = os.getenv('ENABLE_LOGGING', 'true').lower() == 'true'

if logging_enabled:
    handler = RotatingFileHandler(log_file_path, maxBytes=max_log_file_size, backupCount=backup_count)
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        handlers=[handler])
else:
    logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)

logger.info("日志配置已完成，并启用了大小限制")
# 要监听的文件夹
FOLDERS = ["INBOX", "Junk"]
LOGIN_SUCCESS = []
EVC = None
EVC = os.getenv('ENABLE_EVC', 'true').lower() == 'true'

def load_config():
    import config
    return config

def decode_part(part):
    charset = part.get_content_charset()
    payload = part.get_payload(decode=True)

    if not charset:
        result = chardet.detect(payload)
        charset = result['encoding']

    try:
        return payload.decode(charset, errors='ignore')
    except Exception as e:
        logging.error(f"解码邮件部分内容时出错: {str(e)}")
        return ""

def get_forwarded_email_info(email_message, account_email):
    # 检查邮件头是否包含转发信息
    delivered_to = email_message.get('Delivered-To')
    original_to = email_message.get('X-Original-To')
    return_path = email_message.get('Return-Path')
    to = email_message.get('To')

    if (delivered_to and delivered_to != account_email) or \
            (original_to and original_to != account_email) or \
            (to and to != account_email):
        # 邮件是转发的，返回原始收件人和发件人信息
        original_recipient = delivered_to or original_to or to
        original_sender = email_message.get('From')
        return original_recipient, original_sender
    else:
        # 邮件不是转发的，返回None
        return None, None

def escape_html(text):
    if not isinstance(text, str):
        text = str(text)
    return html.escape(text)

def clean_html_content(html_content):
    soup = BeautifulSoup(html_content, "html.parser")

    # 删除不需要的标签
    for tag in soup(["script", "style", "meta", "noscript", "head", "title", "link", "iframe"]):
        tag.extract()

    # 删除空标签
    for tag in soup.find_all():
        if not tag.text.strip():
            tag.extract()

    # 获取清理后的文本
    text = soup.get_text(separator='\n')

    # 移除多余的换行和空白行
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()

    return text

def html_to_text(part):
    try:
        html_content = part.get_payload(decode=True).decode(errors='ignore')
        cleaned_text = clean_html_content(html_content)
        return cleaned_text
    except Exception as e:
        logging.error(f"提取HTML内容时出错: {str(e)}")
        return ""

def get_email_content(email_message):
    logger.debug("开始提取邮件内容")
    content = ""

    if email_message.is_multipart():
        logger.debug("邮件是多部分格式")
        for part in email_message.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            logger.debug(f"处理邮件部分 - 类型: {content_type}, 处置: {content_disposition}")

            if content_type == "text/html" and "attachment" not in content_disposition:
                logger.debug("尝试提取HTML内容")
                content = html_to_text(part)
                if content:
                    logger.debug("成功提取HTML内容")
                    break

        if not content:
            logger.debug("未找到HTML内容,尝试提取纯文本内容")
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))
                logger.debug(f"处理邮件部分 - 类型: {content_type}, 处置: {content_disposition}")

                if content_type == "text/plain" and "attachment" not in content_disposition:
                    logger.debug("提取纯文本内容")
                    content = decode_part(part)
                    content = re.sub(r'\n\s*\n', '\n\n', content).strip()
                    break
    else:
        logger.debug("邮件是单一部分格式")
        content = decode_part(email_message)
        content = re.sub(r'\n\s*\n', '\n\n', content).strip()

    logger.debug(f"提取的内容长度: {len(content)}")

    # 双重清理以去除所有潜在的HTML标签
    content = clean_html_content(content)
    content = html.unescape(content)  # 转义HTML字符

    # 设置最大消息长度（例如3000字符）
    max_length = 3000
    if len(content) > max_length:
        content = content[:max_length] + "..."

    return escape_html(content)



async def send_telegram_message(message, is_junk=False):
    config = load_config()
    TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
    # 未设置 TELEGRAM_JUNK_CHAT_ID 或为空，跳过垃圾邮件转发
    if is_junk and (not hasattr(config, 'TELEGRAM_JUNK_CHAT_ID') or not config.TELEGRAM_JUNK_CHAT_ID):
        logger.info("未设置 TELEGRAM_JUNK_CHAT_ID 或为空，跳过垃圾邮件转发")
        return
    TELEGRAM_CHAT_ID = config.TELEGRAM_JUNK_CHAT_ID if is_junk else config.TELEGRAM_CHAT_ID
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')


def run_in_thread(loop, coro):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(coro)
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


def decode_header_string(header):
    decoded_parts = []
    for part, encoding in decode_header(header):
        if isinstance(part, bytes):
            try:
                decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
            except Exception as e:
                logger.error(f"解码头部时出错: {e}", exc_info=True)
                decoded_parts.append(part.decode('utf-8', errors='replace'))
        else:
            decoded_parts.append(part)
    return ''.join(decoded_parts)


def fetch_email(server, msg_num, email_config, folder_name, retry_count=0):
    MAX_RETRIES = 3
    logger.debug(f"开始获取邮件 - 文件夹: {folder_name}, 邮件号: {msg_num}, 重试次数: {retry_count}")

    try:
        logger.debug(f"尝试从服务器获取邮件数据")
        status, data = server.fetch(msg_num, "(RFC822)")
        logger.debug(f"服务器返回状态: {status}, 数据长度: {len(data) if data else 'None'}")

        if status != "OK":
            raise Exception(f"获取邮件失败,状态: {status}")

        if len(data) == 0 or not isinstance(data[0], tuple):
            raise Exception(f"意外的数据格式: {data}")

        if len(data[0]) < 2 or not isinstance(data[0][1], bytes):
            raise Exception(f"无效的数据结构: {data}")

        bytes_data = data[0][1]
        logger.debug(f"成功获取邮件数据, 大小: {len(bytes_data)} bytes")

        logger.debug("开始解析邮件数据")
        email_message = email.message_from_bytes(bytes_data)
        logger.debug(f"邮件解析完成")

        # 记录原始邮件头
        logger.debug(f"原始邮件头:\n{email_message.as_string()}")

        email_date = parsedate_to_datetime(email_message['Date'])

        timezone_str = os.environ.get('TIMEZONE', 'Asia/Shanghai')
        try:
            email_timezone = pytz.timezone(timezone_str)
        except pytz.UnknownTimeZoneError:
            logger.error(f"Invalid timezone specified: {timezone_str}. Using 'Asia/Shanghai' instead.")
            email_timezone = pytz.timezone('Asia/Shanghai')

        email_time = email_date.astimezone(email_timezone)
        formatted_time = email_time.strftime('%Y-%m-%d %H:%M:%S')

        logging.info(f"处理邮件，日期: {formatted_time}")

        logger.debug("开始处理邮件头")

        subject = email_message["Subject"]
        logger.debug(f"原始主题: {subject}")
        subject = decode_header_string(subject)
        logger.debug(f"解码后的主题: {subject}")

        from_ = email_message["From"]
        logger.debug(f"原始发件人: {from_}")
        from_ = decode_header_string(from_)
        logger.debug(f"解码后的发件人: {from_}")

        original_recipient, original_sender = get_forwarded_email_info(email_message, email_config['EMAIL'])

        if original_recipient and original_sender:
            account_email = original_recipient
            from_ = original_sender
        else:
            account_email = email_config['EMAIL']

        content = get_email_content(email_message)

        # 提取验证码
        if EVC:
            extract_verification_code(content)
            # if vc:
            #     upload(vc)

        language = os.environ.get('LANGUAGE', 'Chinese')

        subject = escape_html(subject)


        # subject = escape_html(subject)
        language_map = {}
        if language == 'English':
            language_map['new_email'] = 'New email from '
            language_map['sender'] = 'Sender: '
            language_map['subject'] = 'Subject: '
            language_map['date'] = 'Date: '
            language_map['content'] = 'Content:\n'
        else:
            language_map['new_email'] = '新邮件来自 '
            language_map['sender'] = '发件人: '
            language_map['subject'] = '主题: '
            language_map['date'] = '日期: '
            language_map['content'] = '内容:\n'

        message = (f"<b>{language_map['subject']}</b> {subject}\n"
                   f"<b>{language_map['new_email']}{escape_html(account_email)}:</b>\n"
                   f"<b>{language_map['sender']}</b> {escape_html(from_)}\n"
                   f"<b>{language_map['date']}</b> {formatted_time}\n\n"
                   f"<b>{language_map['content']}</b>\n{content}")

        is_junk = folder_name.lower() != "inbox"
        logger.debug(f"is_junk = {is_junk},folder_name = {folder_name}")
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=run_in_thread, args=(loop, send_telegram_message(message,is_junk)))
        t.start()
        t.join()


    except Exception as e:
        logger.error(f"获取邮件时出错: {e}", exc_info=True)
        if "Unexpected data format" in str(e) and retry_count < MAX_RETRIES:
            logger.info(f"准备重试获取操作 ({retry_count + 1}/{MAX_RETRIES})...")
            time.sleep(1)
            return fetch_email(server, msg_num, email_config, folder_name, retry_count + 1)
        else:
            raise



def monitor_email(email_config):
    threads = []
    email_provider = 'gmail' if 'gmail.com' in email_config['EMAIL'] else \
                     'outlook' if 'outlook.com' in email_config['EMAIL'] or 'hotmail.com' in email_config['EMAIL'] else \
                     'other'

    # 如果是 Gmail 或 Outlook，直接监听 INBOX 和 Junk
    if email_provider in ['gmail', 'outlook']:
        folders_to_monitor = FOLDERS  # 监听 INBOX 和 Junk
    else:
        # 否则尝试从 IMAP 服务器获取文件夹列表
        folders_to_monitor = ["INBOX"]  # 默认只监听 INBOX
        try:
            server = imaplib2.IMAP4_SSL(email_config['IMAP_SERVER'], email_config['IMAP_SERVER_PORT'])
            server.login(email_config['EMAIL'], email_config['PASSWORD'])
            status, folders = server.list()

            if status == "OK" and folders:
                # 尝试查找类似 Junk 的文件夹
                junk_folder_candidates = ["Junk", "Spam", "垃圾邮件"]
                for folder in folders:
                    folder_name = folder.decode().split(' "/" ')[-1].strip('"')
                    if any(candidate.lower() in folder_name.lower() for candidate in junk_folder_candidates):
                        folders_to_monitor.append(folder_name)
                        break  # 找到类似的 Junk 文件夹后跳出

            server.logout()
        except Exception as e:
            logger.error(f"无法获取文件夹列表: {e}")
            # 如果出错，继续仅监听 INBOX

    for folder in folders_to_monitor:
        thread = threading.Thread(target=idle_mail_listener, args=(email_config, folder))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()


def get_folder_name(folders, folder_type, email_provider):
    if email_provider in ['gmail', 'outlook']:
        for f in folders:
            decoded = f.decode()
            if f'\\{folder_type}' in decoded:
                match = re.search(r'"([^"]*)"$', decoded)
                if match:
                    return match.group(1)
    else:
        # For other providers, we'll use the folder_type directly
        return folder_type
    return None


def idle_mail_listener(email_config, folder):
    config = load_config()
    RETRY_LIMIT = config.RETRY_LIMIT
    RECONNECT_INTERVAL = config.RECONNECT_INTERVAL
    RETRY_DELAY = config.RETRY_DELAY
    RETRY_PAUSE = config.RETRY_PAUSE  # 新增：重试暂停时间
    retry_count = 0
    connection_reset_count = 0  # 重置连接次数计数

    email_provider = 'gmail' if 'gmail.com' in email_config['EMAIL'] else 'other'

    while retry_count < RETRY_LIMIT:
        server = None
        last_reconnect_time = time.time()  # 记录最后一次重新连接的时间

        try:
            # 如果已经重试超过3次，则强制断开并重连
            if connection_reset_count >= 3:
                logger.info(f"{email_config['EMAIL']} 的 {folder} 文件夹: 尝试彻底断开并重连。")
                if server:
                    try:
                        server.logout()
                    except:
                        pass
                connection_reset_count = 0  # 重置计数

            # 建立新的连接
            server = imaplib2.IMAP4_SSL(email_config['IMAP_SERVER'], email_config['IMAP_SERVER_PORT'])
            server.login(email_config['EMAIL'], email_config['PASSWORD'])
            logger.info(f"成功登录邮箱 {email_config['EMAIL']}")

            # 获取并选择文件夹
            _, folders = server.list()
            actual_folder = get_folder_name(folders, folder, email_provider) or folder
            status, messages = server.select(actual_folder)
            if status != "OK":
                raise Exception(f"无法选择文件夹 {actual_folder}: {messages}")
            logger.info(f"成功选择文件夹: {actual_folder}")

            # 获取语言设置
            language = os.environ.get('LANGUAGE', 'Chinese')  # 默认中文

            # 定义语言映射
            if email_config['EMAIL'] not in LOGIN_SUCCESS:
                language_map = {}
                if language == 'English':
                    language_map['sm'] = 'Successfully logged into mailbox ' + email_config['EMAIL']
                else:  # Chinese
                    if 'gmail' in actual_folder.lower():
                        actual_folder = 'Junk'
                    language_map['sm'] = '成功登录邮箱 ' + email_config['EMAIL']

                LOGIN_SUCCESS.append(email_config['EMAIL'])
                # 使用语言映射构建消息
                message = language_map['sm']
                asyncio.run(send_telegram_message(message))

            # 初始化邮件数量
            _, msgnums = server.search(None, "ALL")
            last_email_count = len(msgnums[0].split())
            logger.debug(f"{email_config['EMAIL']} 的 {actual_folder} 中的初始邮件数量: {last_email_count}")

            stop_event = Event()

            while not stop_event.is_set():
                try:
                    current_time = time.time()

                    # 每隔 RECONNECT_INTERVAL 时间主动断开并重连
                    if current_time - last_reconnect_time > RECONNECT_INTERVAL:
                        logger.info(f"{email_config['EMAIL']} 的 {actual_folder} 文件夹主动重新连接中...")
                        last_reconnect_time = time.time()  # 更新最后一次重连时间
                        break  # 跳出内层循环，进行重新连接

                    server.idle(timeout=60)

                    # 检查新邮件
                    _, msgnums = server.search(None, "ALL")
                    email_count = len(msgnums[0].split())

                    if email_count > last_email_count:
                        new_emails = range(last_email_count + 1, email_count + 1)
                        for num in new_emails:
                            fetch_email(server, str(num), email_config, actual_folder)
                        last_email_count = email_count

                    retry_count = 0  # 重置重试计数
                    connection_reset_count = 0  # 成功运行后，重置连接计数

                except OSError as e:
                    logger.error(f"{email_config['EMAIL']} 的 {actual_folder} 文件夹IDLE循环中出错: {e}")
                    retry_count += 1
                    connection_reset_count += 1  # 增加连接重置计数

                    # 如果重试次数达到限制或连接重置计数达到3，则跳出内层循环以进行彻底重连
                    if retry_count >= RETRY_LIMIT or connection_reset_count >= 3:
                        break

                except Exception as e:
                    logger.error(f"Error in IDLE loop for {email_config['EMAIL']} - {actual_folder}: {e}")
                    retry_count += 1
                    connection_reset_count += 1  # 增加连接重置计数

                    if retry_count >= RETRY_LIMIT or connection_reset_count >= 3:
                        break

        except Exception as e:
            logger.error(f"{email_config['EMAIL']} 的 {folder} 文件夹idle_mail_listener中出错: {e}")
            retry_count += 1
            connection_reset_count += 1  # 增加连接重置计数

        finally:
            if server:
                try:
                    logger.debug("断开重连")
                    server.logout()
                except:
                    pass

        # 如果达到最大重试次数，发送错误消息并暂停
        if retry_count >= RETRY_LIMIT:
            final_message = f"{email_config['EMAIL']} 的 {folder} 文件夹重试次数达到上限。等待 {RETRY_PAUSE // 60} 分钟后重试。"
            logger.error(final_message)
            asyncio.run(send_telegram_message(final_message))
            time.sleep(RETRY_PAUSE)  # 等待指定时间后重试
            retry_count = 0  # 重置重试计数
        else:
            time.sleep(RETRY_DELAY)  # 等待一段时间后再重试




def main():
    config = load_config()
    logger.info("开始监听多个邮箱账户")

    email_threads = []
    for email_config in config.EMAILS:
        thread = threading.Thread(target=monitor_email, args=(email_config,))
        email_threads.append(thread)
        thread.start()

    while any(thread.is_alive() for thread in email_threads):
        time.sleep(60)  # 每分钟检查一次

    logger.info("所有邮件监控线程已停止。程序退出。")
if __name__ == "__main__":
    main()
