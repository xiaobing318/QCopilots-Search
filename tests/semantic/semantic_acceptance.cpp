#include "core/runtime_paths.h"
#include "extraction/extractor.h"
#include "runtime/EmbeddingRuntime.h"
#include "storage/index_repository.h"
#include "support/test_environment.h"
#include <QCommandLineParser>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QImage>
#include <QJsonDocument>
#include <QPainter>
#include <QPainterPath>
#include <QPdfWriter>
#include <QRandomGenerator>
#include <QSaveFile>
#include <QSet>
#include <QStandardPaths>
#include <QTextStream>
#include <QtEndian>
#include <algorithm>
#include <cmath>
#include <stdexcept>

using namespace qcopilots_search;
namespace {
constexpr double pi = 3.14159265358979323846;
struct Query {
    QString text, language, modality, file;
    bool code = false;
};
struct Input {
    QString file, modality;
    bool code = false;
};
struct Prepared {
    QList<Input> files;
    QList<Query> queries;
};
struct Vector {
    QString file;
    bool code;
    QVector<float> value;
};
void require(bool value, const QString &error) {
    if (!value)
        throw std::runtime_error(error.toStdString());
}
QByteArray read(const QString &path) {
    QFile f(path);
    require(f.open(QIODevice::ReadOnly), "Cannot read fixture: " + path + " " + f.errorString());
    return f.readAll();
}
void write(const QString &path, const QByteArray &bytes) {
    QSaveFile f(path);
    require(f.open(QIODevice::WriteOnly), "Cannot write: " + path);
    require(f.write(bytes) == bytes.size() && f.commit(), "Cannot commit fixture: " + path);
}
QString fixtureRoot() { return QStringLiteral(QCOPILOTS_SEARCH_SEMANTIC_FIXTURES); }
void validateAssets() {
    const auto manifest = QJsonDocument::fromJson(read(fixtureRoot() + "/assets.json")).object();
    require(manifest.value("schema").toInt() == 1, "Invalid semantic fixture schema");
    for (const auto &entry : manifest.value("assets").toArray()) {
        const auto item = entry.toObject();
        const QString name = item.value("path").toString();
        require(!name.isEmpty() && !name.contains("..") && QDir::isRelativePath(name), "Unsafe asset path");
        auto bytes = read(fixtureRoot() + "/" + name);
        require(bytes.size() == item.value("bytes").toInteger(), "Wrong fixture size: " + name);
        require(QString::fromLatin1(QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex()) ==
                    item.value("sha256").toString(),
                "Wrong fixture SHA-256: " + name);
    }
}
QList<QStringList> topics() {
    QList<QStringList> result;
    const auto lines = QString::fromUtf8(read(fixtureRoot() + "/topics.tsv")).split('\n');
    QSet<QString> ids;
    for (int i = 1; i < lines.size(); ++i) {
        if (lines[i].trimmed().isEmpty())
            continue;
        auto cols = lines[i].trimmed().split('\t');
        require(cols.size() == 4 && !ids.contains(cols[0]),
                "Invalid or duplicate topic row " + QString::number(i + 1));
        for (const auto &col : cols)
            require(!col.isEmpty(), "Empty topic field");
        ids.insert(cols[0]);
        result.append(cols);
    }
    require(result.size() == 80, "Expected 80 independently authored topics");
    return result;
}
QString opaque(const QString &root, int number, const QString &extension) {
    // File names deliberately contain neither the answer nor a query term.
    return root + "/" + QString::number(number, 16).rightJustified(8, '0') + "." + extension;
}
void add(Prepared &out, const QString &file, const QString &modality, const QString &en, const QString &zh,
         bool code = false) {
    out.files.append({file, modality, code});
    out.queries.append({en, "en", modality, file, code});
    out.queries.append({zh, "zh", modality, file, code});
}
void pdf(const QString &path, const QString &text) {
    QPdfWriter writer(path);
    writer.setResolution(144);
    writer.setPageSize(QPageSize(QPageSize::A4));
    QPainter painter(&writer);
    require(painter.isActive(), "Cannot paint PDF");
    painter.setFont(QFont("sans-serif", 18));
    painter.drawText(QRect(100, 100, 950, 1300), Qt::TextWordWrap, text);
    painter.end();
}
QImage illustration(int scene) {
    QImage image(640, 480, QImage::Format_RGB32);
    image.fill(Qt::white);
    QPainter p(&image);
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(Qt::NoPen);
    if (scene == 0) {
        p.setBrush(QColor("#e52b25"));
        p.drawEllipse(QRect(150, 70, 340, 340));
    }
    if (scene == 1) {
        image.fill(QColor("#122454"));
        p.setBrush(QColor("#ffdb20"));
        QPolygonF star;
        for (int i = 0; i < 10; ++i) {
            double a = -pi / 2 + i * pi / 5;
            double r = i % 2 ? 80 : 190;
            star << QPointF(320 + r * cos(a), 240 + r * sin(a));
        }
        p.drawPolygon(star);
    }
    if (scene == 2) {
        image.fill(QColor("#d9eefb"));
        p.fillRect(0, 340, 640, 140, QColor("#699341"));
        for (int x : {90, 210, 350, 500}) {
            p.fillRect(x - 12, 250, 24, 130, QColor("#745131"));
            p.setBrush(QColor("#166434"));
            p.drawPolygon(QPolygonF{QPointF(x, 60), QPointF(x - 85, 320), QPointF(x + 85, 320)});
        }
    }
    if (scene == 3) {
        image.fill(QColor("#b8eaff"));
        p.fillRect(0, 280, 640, 200, QColor("#267ecc"));
        p.setBrush(QColor("#603a25"));
        p.drawPolygon(QPolygonF{QPointF(130, 290), QPointF(500, 290), QPointF(450, 360), QPointF(190, 360)});
        p.fillRect(314, 80, 8, 210, QColor("#333333"));
        p.setBrush(QColor("#f24238"));
        p.drawPolygon(QPolygonF{QPointF(308, 100), QPointF(160, 270), QPointF(308, 270)});
    }
    if (scene == 4) {
        image.fill(QColor("#a3d2f4"));
        p.setBrush(QColor("#ffcb25"));
        p.drawEllipse(QRect(480, 30, 100, 100));
        p.setBrush(QColor("#7a7f82"));
        p.drawPolygon(QPolygonF{QPointF(0, 480), QPointF(290, 45), QPointF(600, 480)});
        p.setBrush(Qt::white);
        p.drawPolygon(QPolygonF{QPointF(210, 165), QPointF(290, 45), QPointF(375, 165), QPointF(315, 145),
                                QPointF(275, 175)});
    }
    if (scene == 5) {
        image.fill(QColor("#b9dff7"));
        p.fillRect(0, 395, 640, 85, Qt::white);
        p.setBrush(Qt::white);
        p.drawEllipse(QRect(200, 235, 245, 225));
        p.drawEllipse(QRect(230, 100, 185, 185));
        p.setBrush(Qt::black);
        p.drawEllipse(QRect(275, 158, 15, 15));
        p.drawEllipse(QRect(353, 158, 15, 15));
        p.fillRect(250, 80, 150, 30, Qt::black);
        p.fillRect(277, 25, 95, 80, Qt::black);
        p.setBrush(QColor("#ec831a"));
        p.drawPolygon(QPolygonF{QPointF(320, 185), QPointF(405, 201), QPointF(320, 215)});
    }
    if (scene == 6) {
        p.setBrush(QColor("#d22d2d"));
        p.drawEllipse(QRect(140, 130, 220, 280));
        p.drawEllipse(QRect(290, 130, 210, 280));
        p.fillRect(313, 70, 18, 100, QColor("#6b4729"));
        p.setBrush(QColor("#349034"));
        p.drawEllipse(QRect(333, 65, 100, 45));
    }
    if (scene == 7) {
        for (int y = 0; y < 8; ++y)
            for (int x = 0; x < 8; ++x)
                p.fillRect(80 + x * 60, y * 60, 60, 60, (x + y) % 2 ? Qt::black : Qt::white);
    }
    p.end();
    return image;
}
void le(QByteArray &b, quint32 v) {
    char bytes[4];
    qToLittleEndian(v, reinterpret_cast<uchar *>(bytes));
    b.append(bytes, 4);
}
void le16(QByteArray &b, quint16 v) {
    char bytes[2];
    qToLittleEndian(v, reinterpret_cast<uchar *>(bytes));
    b.append(bytes, 2);
}
QByteArray chunk(const char *tag, QByteArray data) {
    QByteArray b(tag, 4);
    le(b, quint32(data.size()));
    b += data;
    if (data.size() % 2)
        b += '\0';
    return b;
}
QByteArray list(const char *tag, const QByteArray &data) { return chunk("LIST", QByteArray(tag, 4) + data); }
void video(const QString &path, int scene) {
    // Original six-second uncompressed AVI. The production FFmpeg decoder must
    // decode its real frames and timestamps, including the moving object.
    constexpr int width = 320, height = 240, fps = 4, frames = 24, size = width * height * 3;
    QByteArray avih;
    for (quint32 v : {250000u, quint32(size * fps), 0u, 0x10u, quint32(frames), 0u, 1u, quint32(size),
                      quint32(width), quint32(height), 0u, 0u, 0u, 0u})
        le(avih, v);
    QByteArray strh("vidsDIB ", 8);
    for (quint32 v : {0u, 0u, 0u, 1u, quint32(fps), 0u, quint32(frames), quint32(size), 0xffffffffu, 0u})
        le(strh, v);
    le16(strh, 0);
    le16(strh, 0);
    le16(strh, width);
    le16(strh, height);
    QByteArray strf;
    le(strf, 40);
    le(strf, width);
    le(strf, height);
    le16(strf, 1);
    le16(strf, 24);
    for (quint32 v : {0u, quint32(size), 0u, 0u, 0u, 0u})
        le(strf, v);
    QByteArray movi, index;
    quint32 offset = 4;
    for (int i = 0; i < frames; ++i) {
        QImage im(width, height, QImage::Format_RGB32);
        im.fill(Qt::white);
        QPainter p(&im);
        p.setPen(Qt::NoPen);
        p.setBrush(scene ? Qt::blue : Qt::red);
        int x = 10 + i * 10;
        int y = scene ? int(80 + 55 * sin(i * 0.5)) : 80;
        if (scene)
            p.drawRect(x, y, 50, 50);
        else
            p.drawEllipse(x, y, 50, 50);
        p.end();
        QByteArray raw;
        raw.reserve(size);
        for (int yy = height - 1; yy >= 0; --yy)
            for (int xx = 0; xx < width; ++xx) {
                QRgb c = im.pixel(xx, yy);
                raw += char(qBlue(c));
                raw += char(qGreen(c));
                raw += char(qRed(c));
            }
        movi += chunk("00db", raw);
        index += "00db";
        le(index, 0x10);
        le(index, offset);
        le(index, size);
        offset += size + 8;
    }
    auto data = QByteArray("AVI ", 4) +
                list("hdrl", chunk("avih", avih) + list("strl", chunk("strh", strh) + chunk("strf", strf))) +
                list("movi", movi) + chunk("idx1", index);
    write(path, chunk("RIFF", data));
}
void tone(const QString &path, bool alarm) {
    QByteArray pcm;
    constexpr int sampleRate = 16000;
    for (int i = 0; i < sampleRate * 6; ++i) {
        double t = double(i) / sampleRate;
        double amplitude = alarm ? (std::fmod(t, 0.7) < 0.35 ? 0.45 : 0.0) : 0.35;
        double hz = alarm ? 880 : 440;
        le16(pcm, quint16(qint16(std::sin(2 * pi * hz * t) * amplitude * 32767)));
    }
    QByteArray format;
    le16(format, 1);
    le16(format, 1);
    le(format, sampleRate);
    le(format, sampleRate * 2);
    le16(format, 2);
    le16(format, 16);
    write(path, chunk("RIFF", QByteArray("WAVE", 4) + chunk("fmt ", format) + chunk("data", pcm)));
}
Prepared prepare(const QString &root, bool includeBackground) {
    Prepared out;
    int number = 1;
    for (const auto &row : topics()) {
        const bool isPdf = number <= 4;
        QString file = opaque(root, number++, isPdf ? "pdf" : "txt");
        if (isPdf)
            pdf(file, row[1]);
        else
            write(file, row[1].toUtf8());
        add(out, file, isPdf ? "pdf" : "text", row[2], row[3]);
    }
    const QList<QStringList> pictures = {
        {"A large red circle on a white background", "白色背景上的大红色圆形"},
        {"A yellow five pointed star on a dark blue background", "深蓝背景上的黄色五角星"},
        {"An illustration of several green pine trees in a forest", "几棵绿色松树组成的森林插画"},
        {"A sailing boat with a red triangular sail on blue water", "蓝色水面上有红色三角帆的小船"},
        {"A snowy mountain peak beneath a yellow sun", "黄色太阳下覆盖白雪的山峰"},
        {"A snowman wearing a black hat with an orange carrot nose", "戴黑帽子并有橙色胡萝卜鼻子的雪人"},
        {"A red apple with a brown stem and a green leaf", "带棕色果柄和绿色叶子的红苹果"},
        {"A board with alternating black and white squares", "黑白方格交替排列的棋盘"}};
    for (int i = 0; i < pictures.size(); ++i) {
        auto file = opaque(root, number++, "png");
        require(illustration(i).save(file), "Cannot save original illustration");
        add(out, file, "image", pictures[i][0], pictures[i][1]);
    }
    const QList<QStringList> ocr = {
        {"LIBRARY NOTICE\nClosed on Monday\nPlease return books on Tuesday",
         "A library notice saying it is closed on Monday", "图书馆星期一闭馆的告示"},
        {"TRAIN DEPARTURE\nPlatform 3\nDeparture 09:45\nDestination: Riverside",
         "Train departure information for Riverside at 09:45", "九点四十五分开往河滨的列车信息"},
        {"安全出口\n请勿堆放物品\n保持通道畅通",
         "A Chinese safety sign asking people to keep the emergency exit clear",
         "提醒不要堆物、保持安全出口畅通的标牌"},
        {"社区会议通知\n地点：二楼活动室\n时间：星期六下午三点",
         "A Chinese notice for a community meeting on Saturday afternoon",
         "星期六下午三点在二楼活动室召开社区会议"}};
    for (const auto &item : ocr) {
        QImage im(1200, 700, QImage::Format_RGB32);
        im.fill(Qt::white);
        QPainter p(&im);
        p.setPen(Qt::black);
        p.setFont(QFont("sans-serif", 34));
        p.drawText(QRect(50, 50, 1100, 600), Qt::TextWordWrap, item[0]);
        p.end();
        auto file = opaque(root, number++, "png");
        require(im.save(file), "Cannot save OCR fixture");
        add(out, file, "ocr", item[1], item[2]);
    }
    const QList<QStringList> codes = {
        {"def find_sorted(items, value):\n    low, high = 0, len(items)\n    while low < high:\n        "
         "middle = (low + high) // 2\n        if items[middle] < value:\n            low = middle + 1\n      "
         "  else:\n            high = middle\n    return low if low < len(items) and items[low] == value "
         "else -1\n",
         "Find an item in a sorted array using binary search", "使用二分查找在有序数组中定位元素"},
        {"from collections import deque\ndef shortest_path(graph, start, goal):\n    queue = deque([(start, "
         "[start])])\n    visited = {start}\n    while queue:\n        node, path = queue.popleft()\n        "
         "if node == goal:\n            return path\n        for adjacent in graph.get(node, []):\n          "
         "  if adjacent not in visited:\n                visited.add(adjacent)\n                "
         "queue.append((adjacent, path + [adjacent]))\n    return None\n",
         "Breadth first search for a shortest path in an unweighted graph",
         "用广度优先搜索寻找无权图最短路径"},
        {"def merge_ranges(ranges):\n    result = []\n    for start, end in sorted(ranges):\n        if "
         "result and start <= result[-1][1]:\n            result[-1][1] = max(end, result[-1][1])\n        "
         "else:\n            result.append([start, end])\n    return result\n",
         "Combining overlapping numeric intervals", "将重叠的数字区间合并"},
        {"import time\ndef retry(operation, attempts=4):\n    for attempt in range(attempts):\n        "
         "try:\n            return operation()\n        except TimeoutError:\n            if attempt == "
         "attempts - 1:\n                raise\n            time.sleep(0.1 * 2 ** attempt)\n",
         "Retrying a timed out operation with exponential backoff", "操作超时后采用指数退避重试"},
        {"from collections import OrderedDict\nclass Cache:\n    def __init__(self, capacity):\n        "
         "self.capacity, self.items = capacity, OrderedDict()\n    def get(self, key):\n        value = "
         "self.items.pop(key)\n        self.items[key] = value\n        return value\n    def put(self, key, "
         "value):\n        self.items.pop(key, None)\n        self.items[key] = value\n        if "
         "len(self.items) > self.capacity:\n            self.items.popitem(last=False)\n",
         "An LRU cache that discards the least recently used entry", "缓存超出容量时淘汰最近最少使用的条目"},
        {"import hashlib\ndef file_digest(path):\n    digest = hashlib.sha256()\n    with open(path, 'rb') "
         "as stream:\n        while block := stream.read(65536):\n            digest.update(block)\n    "
         "return digest.hexdigest()\n",
         "Computing a file SHA256 checksum without loading the whole file",
         "流式读取文件并计算 SHA256 校验和"}};
    for (const auto &item : codes) {
        auto file = opaque(root, number++, "py");
        write(file, item[0].toUtf8());
        add(out, file, "code", item[1], item[2], true);
    }
    // More than ten unrelated code files make code Recall@10 meaningful. These
    // functions are original test data, not an alternate implementation library.
    const QStringList codeDistractors = {
        "def is_leap_year(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n",
        "def hamming_distance(left, right):\n    return (left ^ right).bit_count()\n",
        "def quote_csv(value):\n    return '\"' + str(value).replace('\"', '\"\"') + '\"'\n",
        "def palindrome(text):\n    clean = ''.join(c.casefold() for c in text if c.isalnum())\n    return "
        "clean == clean[::-1]\n",
        "def factorial(number):\n    answer = 1\n    for value in range(2, number + 1):\n        answer *= "
        "value\n    return answer\n",
        "def fibonacci(count):\n    a, b = 0, 1\n    for _ in range(count):\n        yield a\n        a, b = "
        "b, a + b\n",
        "def clamp(value, lower, upper):\n    return min(upper, max(lower, value))\n",
        "def interpolate(a, b, fraction):\n    return a + (b - a) * fraction\n",
        "def weighted_mean(values, weights):\n    total = sum(weights)\n    if total == 0:\n        raise "
        "ValueError('zero total weight')\n    return sum(v * w for v, w in zip(values, weights)) / total\n",
        "def rotate_clockwise(matrix):\n    return [list(row) for row in zip(*matrix[::-1])]\n",
        "def unique_in_order(values):\n    seen = set()\n    return [v for v in values if not (v in seen or "
        "seen.add(v))]\n",
        "def is_prime(number):\n    if number < 2:\n        return False\n    divisor = 2\n    while divisor "
        "* divisor <= number:\n        if number % divisor == 0:\n            return False\n        divisor "
        "+= 1\n    return True\n",
        "def caesar(text, shift):\n    return ''.join(chr((ord(c)-97+shift)%26+97) if 'a' <= c <= 'z' else c "
        "for c in text)\n",
        "def histogram(values):\n    counts = {}\n    for value in values:\n        counts[value] = "
        "counts.get(value, 0) + 1\n    return counts\n",
        "def flatten(rows):\n    return [value for row in rows for value in row]\n",
        "def transpose(matrix):\n    return [list(column) for column in zip(*matrix)]\n",
        "def degrees_to_radians(degrees):\n    import math\n    return degrees * math.pi / 180\n",
        "def normalize_vector(vector):\n    import math\n    norm = math.sqrt(sum(x*x for x in vector))\n    "
        "return [x/norm for x in vector] if norm else list(vector)\n",
        "def run_length_encode(values):\n    from itertools import groupby\n    return [(value, sum(1 for _ "
        "in group)) for value, group in groupby(values)]\n",
        "def parse_hex_color(text):\n    value = text.removeprefix('#')\n    if len(value) != 6:\n        "
        "raise ValueError('expected six hex digits')\n    return tuple(int(value[i:i+2], 16) for i in (0, 2, "
        "4))\n",
        "def balanced_brackets(text):\n    stack = []\n    pairs = {')':'(', ']':'[', '}':'{'}\n    for char "
        "in text:\n        if char in '([{':\n            stack.append(char)\n        elif char in pairs and "
        "(not stack or stack.pop() != pairs[char]):\n            return False\n    return not stack\n",
        "def triangle_area(base, height):\n    return base * height / 2\n",
        "def format_duration(seconds):\n    minutes, remainder = divmod(seconds, 60)\n    hours, minutes = "
        "divmod(minutes, 60)\n    return f'{hours:02}:{minutes:02}:{remainder:02}'\n",
        "def dot_product(a, b):\n    if len(a) != len(b):\n        raise ValueError('dimension mismatch')\n  "
        "  return sum(x*y for x, y in zip(a, b))\n"};
    for (const auto &source : codeDistractors) {
        auto file = opaque(root, number++, "py");
        write(file, source.toUtf8());
        out.files.append({file, "code-background", true});
    }
    const QList<QStringList> speech = {
        {"An announcement that the airport train is delayed by a fallen tree",
         "树木倒在铁路上导致机场列车延误的广播"},
        {"A spoken reminder to buy milk, eggs and bread", "提醒购买牛奶、鸡蛋和面包的语音"},
        {"Museum opening hours and the time for a guided tour", "说明博物馆开放时间和导览开始时间的录音"},
        {"Someone explaining how to bake a chocolate cake", "有人讲解巧克力蛋糕的烘焙方法"}};
    for (int i = 0; i < speech.size(); ++i) {
        auto file = opaque(root, number++, "wav");
        write(file, read(fixtureRoot() + "/assets/speech-" + QString::number(i + 1).rightJustified(3, '0') +
                         ".wav"));
        add(out, file, "speech", speech[i][0], speech[i][1]);
    }
    for (int i = 0; i < 2; ++i) {
        auto file = opaque(root, number++, "wav");
        tone(file, i);
        add(out, file, "environment",
            i ? "An electronic alarm making repeated high beeps" : "A steady continuous pure musical tone",
            i ? "电子警报器反复发出高音蜂鸣" : "持续稳定不间断的纯音");
    }
    for (int i = 0; i < 2; ++i) {
        auto file = opaque(root, number++, "avi");
        video(file, i);
        add(out, file, "video",
            i ? "A blue square moving and bouncing across a white screen"
              : "A red ball travelling from left to right across a white screen",
            i ? "蓝色方块在白色画面中移动并上下跳动" : "红色圆球在白色画面中从左向右移动");
    }
    const QList<QStringList> natural = {
        {"photo-001.jpg", "image-natural",
         "Planet Earth seen from space with blue oceans, white clouds and continents",
         "从太空看到的地球，蓝色海洋、白色云层和大陆"},
        {"photo-002.jpg", "image-natural", "A domestic cat walking through white snow outdoors",
         "一只家猫在户外白色雪地上行走"},
        {"environment-001.ogg", "environment", "A real dog barking repeatedly", "一只狗反复吠叫的真实录音"},
        {"environment-002.wav", "environment", "Rain and wind recorded outside during wet weather",
         "在户外录到的下雨和刮风声音"},
        {"video-001.webm", "video-natural", "A video of heavy rain falling outdoors",
         "户外大雨不断落下的实拍视频"}};
    for (const auto &item : natural) {
        auto file = opaque(root, number++, QFileInfo(item[0]).suffix());
        write(file, read(fixtureRoot() + "/assets/" + item[0]));
        add(out, file, item[1], item[2], item[3]);
    }
    if (includeBackground)
        for (int i = 1; i <= 3; ++i) {
            auto file = opaque(root, number++, "txt");
            write(file,
                  read(fixtureRoot() + "/assets/book-" + QString::number(i).rightJustified(3, '0') + ".txt"));
            out.files.append({file, "background", false});
        }
    require(out.queries.size() >= 200, "Semantic corpus needs at least 200 positive queries");
    return out;
}
double cosine(const QVector<float> &a, const QVector<float> &b) {
    double dot = 0, aa = 0, bb = 0;
    for (qsizetype i = 0; i < a.size(); ++i) {
        dot += double(a[i]) * b[i];
        aa += double(a[i]) * a[i];
        bb += double(b[i]) * b[i];
    }
    return dot / std::sqrt(aa * bb);
}
QJsonArray ranking(const QVector<Vector> &vectors, const QVector<float> &query, bool code) {
    QHash<QString, double> best;
    for (const auto &v : vectors)
        if (v.code == code) {
            double score = cosine(v.value, query);
            if (!best.contains(v.file) || best[v.file] < score)
                best[v.file] = score;
        }
    QList<QPair<QString, double>> ordered;
    for (auto i = best.cbegin(); i != best.cend(); ++i)
        ordered.append({i.key(), i.value()});
    std::sort(ordered.begin(), ordered.end(), [](auto &a, auto &b) { return a.second > b.second; });
    QJsonArray out;
    for (int i = 0; i < std::min<qsizetype>(10, ordered.size()); ++i)
        out.append(
            QJsonObject{{"file", QFileInfo(ordered[i].first).fileName()}, {"cosine", ordered[i].second}});
    return out;
}
struct Count {
    int queries = 0, hits = 0, semanticHits = 0;
};
} // namespace

int main(int argc, char **argv) {
    QStandardPaths::setTestModeEnabled(true);
    QGuiApplication app(argc, argv);
    QCommandLineParser parser;
    parser.addHelpOption();
    parser.addOption(
        {"validate-only", "Validate immutable corpus and author-supplied query labels without inference."});
    parser.addOption({"model-dir", "Read-only verified model directory.", "path",
                      qEnvironmentVariable("QCOPILOTS_SEARCH_MODEL_DIR")});
    parser.addOption({"tessdata-dir", "OCR trained data directory.", "path",
                      qEnvironmentVariable("QCOPILOTS_SEARCH_TESSDATA_DIR",
                                           QStringLiteral(QCOPILOTS_SEARCH_SEMANTIC_TESSDATA))});
    parser.addOption({"report", "Write JSON report to this explicit location.", "path"});
    parser.addOption({"preserve", "Retain isolated corpus and index for diagnosis."});
    parser.addOption(
        {"smoke", "Run a small executable pipeline probe. Never qualifies as release acceptance."});
    // QCommandLineParser::process may use a native message box for --help when
    // a Windows QGuiApplication has redirected standard streams. Test utilities
    // must remain noninteractive even under CTest or a build script.
    if (!parser.parse(app.arguments())) {
        QTextStream(stderr) << parser.errorText() << '\n' << parser.helpText();
        return 2;
    }
    if (parser.isSet("help")) {
        QTextStream(stdout) << parser.helpText();
        return 0;
    }
    QJsonObject report{
        {"schema", 1},
        {"passed", false},
        {"startedAt", QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
        {"mode", parser.isSet("smoke") ? "smoke" : "release"},
        {"annotationProtocol",
         "Target notes and bilingual relevance labels were authored together before observing model results. "
         "This is a controlled implementation acceptance set, not an independently human-annotated blind "
         "retrieval benchmark. Corpus and generator hashes permit detection of later changes."},
        {"corpusScope",
         "Original controlled bilingual target notes, simple original illustrations and synthetic "
         "speech/sounds/video, distinct Project Gutenberg literary distractors, and a small attributed "
         "natural photo/sound/video sample. The natural sample does not establish representative coverage of "
         "all environments, photo subjects, languages or accents."}};
    int result = 1;
    std::unique_ptr<test::TestEnvironment> env;
    try {
        validateAssets();
        topics();
        env = std::make_unique<test::TestEnvironment>("semantic 中文 space");
        require(env->isValid(), env->error());
        auto corpus = env->path("corpus");
        require(QDir().mkpath(corpus), "Cannot create isolated corpus");
        auto prepared = prepare(corpus, !parser.isSet("smoke"));
        report["positiveQueries"] = prepared.queries.size();
        report["fixtures"] = prepared.files.size();
        report["fixtureManifestSha256"] = QString::fromLatin1(
            QCryptographicHash::hash(read(fixtureRoot() + "/assets.json"), QCryptographicHash::Sha256)
                .toHex());
        report["topicManifestSha256"] = QString::fromLatin1(
            QCryptographicHash::hash(read(fixtureRoot() + "/topics.tsv"), QCryptographicHash::Sha256)
                .toHex());
        report["generatorSha256"] =
            QString::fromLatin1(QCryptographicHash::hash(read(fixtureRoot() + "/semantic_acceptance.cpp"),
                                                         QCryptographicHash::Sha256)
                                    .toHex());
        if (parser.isSet("validate-only")) {
            report["status"] = "manifest-passed";
            result = 0;
        } else {
            require(!parser.value("model-dir").isEmpty(),
                    "A verified model directory is required. Missing models fail this acceptance test.");
            EmbeddingRuntime runtime;
            QString error;
            runtime.setThreadCount(4);
            require(runtime.load(parser.value("model-dir"), false, &error), error);
            auto paths = env->runtimePaths();
            require(paths.ensure(&error), error);
            IndexRepository index;
            require(index.open(paths, &error), error);
            require(index.setPipelineFingerprint(
                        EmbeddingRuntime::fingerprint() + "|" + Extractor::pipelineFingerprint(), &error),
                    error);
            QVector<Vector> vectors;
            int extracted = 0;
            QJsonArray files;
            QElapsedTimer timer;
            timer.start();
            for (int n = 0; n < prepared.files.size(); ++n) {
                const auto &input = prepared.files[n];
                if (parser.isSet("smoke") && n > 1)
                    break;
                ExtractionOptions options;
                options.tempDir = env->path("media " + QString::number(n));
                require(QDir().mkpath(options.tempDir), "Cannot create isolated media directory");
                options.tessdataDir = parser.value("tessdata-dir");
                options.code = input.code;
                options.maxSegments = 5000;
                options.chunkCharacters = input.modality == "background" ? 1400 : 1800;
                options.overlapCharacters = 120;
                options.enableOcr = true;
                auto text = Extractor::extract(input.file, options);
                require(text.state == "complete" && !text.segments.isEmpty(),
                        "Extraction failed: " + input.modality + " " + text.state + " " + text.error);
                QVector<Segment> segments;
                QJsonObject extractedModalities;
                qint64 extractedTextCharacters = 0;
                for (auto segment : text.segments) {
                    extractedModalities[segment.modality] =
                        extractedModalities.value(segment.modality).toInt() + 1;
                    extractedTextCharacters += segment.text.size();
                    if (segment.modality == "image" || segment.modality == "video")
                        segment.embedding =
                            runtime.embedImage(segment.mediaPath.isEmpty() ? input.file : segment.mediaPath,
                                               segment.imageBudget, &error);
                    else if (segment.modality == "audio")
                        segment.embedding = runtime.embedAudio(
                            segment.mediaPath.isEmpty() ? input.file : segment.mediaPath, &error);
                    else
                        segment.embedding =
                            runtime.embedText(segment.text, PromptKind::Document, segment.title, &error);
                    require(segment.embedding.size() == 768, "Embedding failed: " + error);
                    vectors.append({input.file, input.code, segment.embedding});
                    segments.append(segment);
                    ++extracted;
                }
                require(
                    index.replaceFiles({IndexedFile{input.file, text.sourceVersion, input.code, segments}},
                                       &error, true),
                    error);
                files.append(QJsonObject{
                    {"file", QFileInfo(input.file).fileName()},
                    {"modality", input.modality},
                    {"segments", segments.size()},
                    {"extractedModalities", extractedModalities},
                    {"extractedTextCharacters", extractedTextCharacters},
                    {"sha256",
                     QString::fromLatin1(
                         QCryptographicHash::hash(read(input.file), QCryptographicHash::Sha256).toHex())}});
                QTextStream(stdout) << "Indexed " << n + 1 << "/" << prepared.files.size() << " files, "
                                    << extracted << " actual segments\n";
                report["actualEmbeddedSegments"] = extracted;
                report["completedFiles"] = n + 1;
            }
            require(index.flush(&error), error);
            report["indexMilliseconds"] = timer.elapsed();
            report["actualEmbeddedSegments"] = extracted;
            report["files"] = files;
            report["modelFingerprint"] = EmbeddingRuntime::fingerprint();
            report["extractionFingerprint"] = Extractor::pipelineFingerprint();
            if (!parser.isSet("smoke"))
                require(extracted >= 1000,
                        "Release corpus must produce at least 1000 actual extracted and embedded segments");
            // The application uses a separate text-only query worker (512 token
            // context). Reload that same mode rather than benchmark a larger
            // document/multimodal context for short search queries.
            runtime.close();
            require(runtime.load(parser.value("model-dir"), true, &error), error);
            QHash<QString, Count> counts;
            QJsonArray results;
            int queryNumber = 0;
            for (const auto &query : prepared.queries) {
                if (parser.isSet("smoke") && queryNumber >= 4)
                    break;
                ++queryNumber;
                timer.restart();
                auto embedding = runtime.embedText(
                    query.text, query.code ? PromptKind::CodeQuery : PromptKind::Query, {}, &error);
                require(embedding.size() == 768, "Query embedding failed: " + error);
                auto encodingMs = timer.elapsed();
                SearchOptions options;
                options.query = query.text;
                options.queryEmbedding = embedding;
                options.code = query.code;
                options.limit = 10;
                timer.restart();
                auto hits = index.search(options, &error);
                require(error.isEmpty(), error);
                auto retrievalMs = timer.elapsed();
                int rank = 0;
                QJsonArray returned;
                for (int i = 0; i < hits.size(); ++i) {
                    returned.append(QJsonObject{{"file", QFileInfo(hits[i].path).fileName()},
                                                {"score", hits[i].score},
                                                {"source", hits[i].matchSource}});
                    if (hits[i].path == query.file)
                        rank = i + 1;
                }
                auto semantic = ranking(vectors, embedding, query.code);
                bool semanticFound = false;
                for (const auto &entry : semantic)
                    if (entry.toObject().value("file").toString() == QFileInfo(query.file).fileName())
                        semanticFound = true;
                for (const auto &group :
                     QStringList{"overall", "language:" + query.language, "modality:" + query.modality}) {
                    auto &c = counts[group];
                    ++c.queries;
                    if (rank > 0)
                        ++c.hits;
                    if (semanticFound)
                        ++c.semanticHits;
                }
                results.append(QJsonObject{{"query", query.text},
                                           {"language", query.language},
                                           {"modality", query.modality},
                                           {"expectedFile", QFileInfo(query.file).fileName()},
                                           {"rank", rank},
                                           {"encodingMilliseconds", encodingMs},
                                           {"retrievalMilliseconds", retrievalMs},
                                           {"productionTop10", returned},
                                           {"exactSemanticTop10", semantic}});
                QTextStream(stdout) << "Query " << queryNumber << "/" << prepared.queries.size()
                                    << " rank=" << rank << " " << query.language << " " << query.modality
                                    << "\n";
            }
            QJsonObject metrics;
            bool passed = !parser.isSet("smoke");
            for (auto i = counts.cbegin(); i != counts.cend(); ++i) {
                double recall = double(i.value().hits) / i.value().queries;
                double gate = i.key() == "overall" ? .90 : .80;
                metrics[i.key()] = QJsonObject{
                    {"queries", i.value().queries},
                    {"hits", i.value().hits},
                    {"recallAt10", recall},
                    {"exactSemanticRecallAt10", double(i.value().semanticHits) / i.value().queries},
                    {"requiredRecallAt10", gate},
                    {"passed", recall >= gate}};
                passed = passed && recall >= gate;
            }
            report["metrics"] = metrics;
            report["queries"] = results;
            const QStringList negatives = {"Telemetry logs from a fusion reactor on Europa",
                                           "A dragon's medical insurance policy number",
                                           "2029 Martian embassy visa appointment password",
                                           "量子传送门的校准许可证编号",
                                           "海王星地下列车的真实时刻表",
                                           "An underwater volcano's encrypted bank account",
                                           "A recipe for transparent anti-gravity concrete",
                                           "会说话的石头购买宇宙飞船的合同",
                                           "A satellite photograph of the city on Pluto",
                                           "永动机工厂的秘密生产配方",
                                           "Minutes of the invisible parliament on Jupiter",
                                           "The checksum of a message sent from a parallel universe",
                                           "半人马座外星船长的家庭住址",
                                           "Teleportation portal maintenance invoice",
                                           "A notarised marriage certificate for a phoenix",
                                           "梦境转换成钻石机器的维修手册",
                                           "A dinosaur's biometric passport issued tomorrow",
                                           "The snow dragon's interstellar tax declaration",
                                           "火星精灵的水下城堡产权证书",
                                           "A shopping receipt from the centre of a black hole"};
            QJsonArray noAnswers;
            QJsonObject noAnswerSummary;
            for (int i = 0; i < negatives.size() && !parser.isSet("smoke"); ++i) {
                auto emb = runtime.embedText(negatives[i], PromptKind::Query, {}, &error);
                require(emb.size() == 768, error);
                SearchOptions opts;
                opts.query = negatives[i];
                opts.queryEmbedding = emb;
                opts.limit = 10;
                auto hits = index.search(opts, &error);
                require(error.isEmpty(), error);
                const QString partition = i < 10 ? "calibration" : "holdout";
                const auto exact = ranking(vectors, emb, false);
                const double topCosine = exact.isEmpty() ? 0.0 :
                    exact.first().toObject().value("cosine").toDouble();
                auto summary = noAnswerSummary.value(partition).toObject();
                const int priorCount = summary.value("queries").toInt();
                summary["queries"] = priorCount + 1;
                summary["queriesReturningCandidates"] =
                    summary.value("queriesReturningCandidates").toInt() + (hits.isEmpty() ? 0 : 1);
                summary["minimumTopCosine"] = priorCount == 0 ? topCosine :
                    qMin(summary.value("minimumTopCosine").toDouble(), topCosine);
                summary["maximumTopCosine"] = priorCount == 0 ? topCosine :
                    qMax(summary.value("maximumTopCosine").toDouble(), topCosine);
                summary["meanTopCosine"] =
                    (summary.value("meanTopCosine").toDouble() * priorCount + topCosine) / (priorCount + 1);
                noAnswerSummary[partition] = summary;
                noAnswers.append(QJsonObject{{"query", negatives[i]},
                                             {"partition", partition},
                                             {"returnedCount", hits.size()},
                                             {"exactSemanticTop10", exact}});
            }
            report["noAnswerQueries"] = noAnswers;
            report["noAnswerSummary"] = noAnswerSummary;
            report["noAnswerInterpretation"] =
                "Independent labelled absent subjects. Search returns ranked candidates and has no "
                "calibrated rejection promise. Raw exact cosine and returned counts are reported separately, "
                "with calibration and untouched holdout groups. No threshold was fitted to positive "
                "acceptance queries.";
            report["status"] =
                parser.isSet("smoke") ? "smoke-passed" : (passed ? "passed" : "quality-gate-failed");
            report["passed"] = passed;
            result = parser.isSet("smoke") || passed ? 0 : 1;
        }
    } catch (const std::exception &exception) {
        report["status"] = "failed";
        report["error"] = QString::fromUtf8(exception.what());
        QTextStream(stderr) << exception.what() << '\n';
    }
    report["finishedAt"] = QDateTime::currentDateTimeUtc().toString(Qt::ISODate);
    QString reportPath = parser.value("report");
    if (reportPath.isEmpty() && !parser.isSet("validate-only"))
        reportPath = qEnvironmentVariable("QCOPILOTS_SEARCH_SEMANTIC_REPORT");
    if (reportPath.isEmpty() && env && !parser.isSet("validate-only"))
        reportPath = env->path("qcs-semantic-report.json");
    if (env && (parser.isSet("preserve") || result != 0 ||
                (!parser.isSet("report") && !parser.isSet("validate-only")))) {
        env->preserve();
        report["preservedRoot"] = env->root();
    }
    // All inference, extraction and storage owners have left scope. Close the
    // isolated test context before reporting success so a cleanup failure is
    // part of the acceptance result rather than a destructor-only warning.
    env.reset();
    const QString cleanupError = test::takeCleanupErrors();
    if (!cleanupError.isEmpty()) {
        result = 1;
        report["status"] = "cleanup-failed";
        report["passed"] = false;
        report["cleanupError"] = cleanupError;
        const QString previous = report.value("error").toString();
        report["error"] = previous.isEmpty() ? cleanupError : previous + "\n" + cleanupError;
        QTextStream(stderr) << cleanupError << '\n';
    }
    if (!reportPath.isEmpty()) {
        report["reportPath"] = reportPath;
        try {
            write(reportPath, QJsonDocument(report).toJson());
        } catch (const std::exception &e) {
            QTextStream(stderr) << e.what() << '\n';
            return 1;
        }
    }
    const QJsonObject summary{{"status", report.value("status")},
                              {"passed", report.value("passed")},
                              {"reportPath", reportPath},
                              {"actualEmbeddedSegments", report.value("actualEmbeddedSegments")},
                              {"positiveQueries", report.value("positiveQueries")},
                              {"metrics", report.value("metrics")},
                              {"error", report.value("error")}};
    QTextStream(stdout) << QJsonDocument(summary).toJson(QJsonDocument::Compact) << '\n';
    return result;
}
