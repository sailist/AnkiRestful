#!/usr/bin/env python3
"""
动态API示例脚本
展示如何使用Anki Restful API的各种功能
"""

import requests
import time


def get_all_decks(base_url):
    """获取所有牌组"""
    response = requests.get(f"{base_url}/api/decks")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取牌组列表失败: {response.status_code}")
        return None


def get_deck_details(base_url, deck_id):
    """获取单个牌组的详细信息"""
    response = requests.get(f"{base_url}/api/decks/{deck_id}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取牌组详情失败: {response.status_code}")
        return None


def get_deck_notes(base_url, deck_id):
    """获取牌组中的所有笔记"""
    response = requests.get(f"{base_url}/api/decks/{deck_id}/notes")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取牌组笔记失败: {response.status_code}")
        return None


def get_all_notes(base_url, page=1, limit=20):
    """获取所有笔记（分页）"""
    params = {"page": page, "limit": limit}
    response = requests.get(f"{base_url}/api/notes", params=params)
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取笔记列表失败: {response.status_code}")
        return None


def get_note_details(base_url, note_id):
    """获取单个笔记的详细信息"""
    response = requests.get(f"{base_url}/api/notes/{note_id}")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取笔记详情失败: {response.status_code}")
        return None


def get_note_cards(base_url, note_id):
    """获取笔记关联的所有卡片"""
    response = requests.get(f"{base_url}/api/notes/{note_id}/cards")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取笔记卡片失败: {response.status_code}")
        return None


def get_note_types(base_url):
    """获取所有笔记类型"""
    response = requests.get(f"{base_url}/api/notetypes")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"获取笔记类型失败: {response.status_code}")
        return None


def restart_api(base_url):
    """重启API服务器（用于热重载）"""
    response = requests.get(f"{base_url}/restart")
    if response.status_code == 200:
        return response.json()
    else:
        print(f"重启API失败: {response.status_code}")
        return None


def main():
    """主函数"""
    base_url = "http://localhost:8102"

    print("🚀 Anki Restful API 动态示例")
    print(f"📍 服务器地址: {base_url}")

    # 等待服务器启动
    print("\n⏳ 等待服务器启动...")
    time.sleep(2)

    try:
        # 1. 获取所有牌组
        print("\n📦 获取所有牌组...")
        decks = get_all_decks(base_url)
        if decks:
            print(f"✅ 找到 {decks['count']} 个牌组")

            # 2. 获取第一个牌组的详细信息
            if decks["decks"]:
                first_deck = decks["decks"][0]
                deck_id = first_deck["id"]
                print(f"\n📋 获取牌组详情: {first_deck['name']} (ID: {deck_id})")
                deck_details = get_deck_details(base_url, deck_id)
                if deck_details:
                    print(f"✅ 牌组详情:")
                    print(f"   - 名称: {deck_details['name']}")
                    print(f"   - 描述: {deck_details.get('description', 'N/A')}")
                    print(f"   - 总卡片数: {deck_details['total_cards']}")
                    print(f"   - 总笔记数: {deck_details['total_notes']}")
                    print(f"   - 复习数: {deck_details['review_count']}")
                    print(f"   - 新卡数: {deck_details['new_count']}")
                    print(f"   - 学习中: {deck_details['learning_count']}")

                # 3. 获取牌组中的笔记
                print(f"\n📝 获取牌组中的笔记...")
                deck_notes = get_deck_notes(base_url, deck_id)
                if deck_notes:
                    print(f"✅ 找到 {deck_notes['count']} 个笔记")
                    # 显示前3个笔记
                    for i, note in enumerate(deck_notes["notes"][:3]):
                        print(f"   {i+1}. {note['model_name']} (ID: {note['id']})")

        # 4. 获取所有笔记（分页）
        print("\n📚 获取笔记列表（第一页）...")
        notes = get_all_notes(base_url, page=1, limit=5)
        if notes:
            print(
                f"✅ 获取到 {len(notes['notes'])} 个笔记（总共 {notes['pagination']['total']} 个）"
            )
            # 显示第一个笔记的详情
            if notes["notes"]:
                first_note = notes["notes"][0]
                note_id = first_note["id"]
                print(f"\n🔍 获取笔记详情: {first_note['model_name']} (ID: {note_id})")
                note_details = get_note_details(base_url, note_id)
                if note_details:
                    print(f"✅ 笔记详情:")
                    print(f"   - 类型: {note_details['model_name']}")
                    print(f"   - 标签: {', '.join(note_details['tags'])}")
                    print(f"   - 字段数: {len(note_details['fields'])}")

                # 5. 获取笔记关联的卡片
                print(f"\n🃏 获取笔记关联的卡片...")
                note_cards = get_note_cards(base_url, note_id)
                if note_cards:
                    print(f"✅ 找到 {note_cards['count']} 张卡片")
                    for i, card in enumerate(note_cards["cards"]):
                        print(
                            f"   {i+1}. 牌组: {card['deck_name']} (类型: {card['type']})"
                        )

        # 6. 获取笔记类型
        print("\n📋 获取笔记类型...")
        note_types = get_note_types(base_url)
        if note_types:
            print(f"✅ 找到 {note_types['count']} 种笔记类型")
            for i, note_type in enumerate(note_types["note_types"][:3]):
                print(
                    f"   {i+1}. {note_type['name']} (字段: {', '.join(note_type['fields'])})"
                )

        # 7. 重启API服务器
        print("\n🔄 重启API服务器...")
        restart_result = restart_api(base_url)
        if restart_result:
            print(f"✅ 重启成功: {restart_result['message']}")

        print("\n🎉 示例完成！")

    except requests.exceptions.ConnectionError:
        print("❌ 无法连接到API服务器")
        print("   💡 请确保:")
        print("      1. Anki正在运行")
        print("      2. 插件已正确安装")
        print("      3. API服务器已启动")
    except Exception as e:
        print(f"❌ 示例执行过程中发生错误: {str(e)}")


if __name__ == "__main__":
    main()
