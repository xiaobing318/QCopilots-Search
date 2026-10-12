#pragma once

#include <QCryptographicHash>
#include <QJsonDocument>
#include <QJsonObject>
#include <QString>

namespace qcopilots_search {

// Bump the contract revision whenever preprocessing, prompts, extraction or
// model behavior changes. Services can inspect these without loading inference
// or parser libraries. Tokenizer/index fingerprints are maintained separately.
inline constexpr char embeddingModelRevision[] = "bfcd298762cc34d0357ece5ebdd31791a3a374d8";
inline constexpr char embeddingTextSha256[] = "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135";
inline constexpr char embeddingMediaSha256[] = "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f";

inline QJsonObject embeddingPipelineContract()
{
    return {
        {QStringLiteral("revision"), 1},
        {QStringLiteral("model"), QStringLiteral("embeddinggemma-2-Q8_0+mmproj-Q8_0")},
        {QStringLiteral("modelRevision"), QString::fromLatin1(embeddingModelRevision)},
        {QStringLiteral("textSha256"), QString::fromLatin1(embeddingTextSha256)},
        {QStringLiteral("mediaSha256"), QString::fromLatin1(embeddingMediaSha256)},
        {QStringLiteral("llamaCpp"), QStringLiteral("4d756bc72bf00a4aacf410ae15a2d315f3db400d+DigUp-no-logits-v1")},
        {QStringLiteral("queryPrompt"), QStringLiteral("task: search result | query: {text}")},
        {QStringLiteral("codeQueryPrompt"), QStringLiteral("task: code retrieval | query: {text}")},
        {QStringLiteral("documentPrompt"), QStringLiteral("title: {title-or-none} | text: {text}")},
        {QStringLiteral("tokenization"), QStringLiteral("UTF-8,BOS+EOS,truncate-preserving-EOS")},
        {QStringLiteral("context"), QStringLiteral("text-query=512,full-multimodal=2048")},
        {QStringLiteral("output"), QStringLiteral("mean-pooling,L2-normalized,768-float-components")},
        {QStringLiteral("image"), QStringLiteral("Pillow-bicubic-22bit-RGB,grid=48,token-budgets=70/140/280/560/1120,mtmd-min=1,max=1120")},
        {QStringLiteral("audio"), QStringLiteral("miniaudio,16kHz,mono,float,truncate=30s,no-prompt")}
    };
}

inline QJsonObject extractionPipelineContract()
{
    return {
        {QStringLiteral("revision"), 3},
        {QStringLiteral("qt"), QStringLiteral("6.12.0,QtPDF-6.140.0")},
        {QStringLiteral("documents"), QStringLiteral("UTF-grapheme-chunks=1800,overlap=200,HTML-no-network,ZIP-XML-miniz-3.1.0,RTF-native-v1")},
        {QStringLiteral("legacyDoc"), QStringLiteral("wv2+POLE-Calligra-88bde0ce71a5c12fb50786017b2e2e07c5ce13e4-port-v1")},
        {QStringLiteral("code"), QStringLiteral("whole-line-chunks=1800,overlap=2-lines,single-line-crop=4000-graphemes,input=1MiB")},
        {QStringLiteral("notebook"), QStringLiteral("nbformat=3/4,input=20MiB,output=1MiB,cell-position=1-based")},
        {QStringLiteral("pdf"), QStringLiteral("native-text>=200-UTF16-or>=24-Unicode-letters-or>=8-Han/Hiragana/Katakana/Hangul-letters,otherwise-render-longest=2000,alpha-composite-white-RGB-before-OCR-and-model,cap=5000-pages,image-budget=280,page=1-based")},
        {QStringLiteral("images"), QStringLiteral("Qt-auto-orientation,ICC-to-sRGB,alpha-composite-white-before-OCR-and-model,ordinary-longest=1024/budget=140,screenshot-longest=1536/budget=280,OCR-cap=6000,HEIF-cap=120M-pixels,RAW-halfsize")},
        {QStringLiteral("screenshotDetection"), QStringLiteral("case-insensitive:Screenshot,Screen Shot,CleanShot,SCR-,截图,屏幕截图,macOS-metadata")},
        {QStringLiteral("ocr"), QStringLiteral("Tesseract-5.5.3,Leptonica-1.87.0,LSTM_ONLY,eng+chi_sim+chi_tra,PSM_AUTO-first")},
        {QStringLiteral("ocrRetry"), QStringLiteral("if-trimmed-empty-or-MeanTextConf<70:PSM_SINGLE_BLOCK-same-size,if-best-still-empty-or-confidence<70-and-short-edge>=64:one-half-size-PSM_SINGLE_BLOCK,half-size-keep-aspect-smooth,select-nonempty-candidate-only-if-best-empty-or-confidence-higher,no-retry-for-confident-input")},
        {QStringLiteral("ocrBudget"), QStringLiteral("at-most-three-passes,shared-single-ETEXT_DESC-120s-deadline-and-cancellation-token,explicit-check-before-and-after-each-pass")},
        {QStringLiteral("tessdataFast"), QStringLiteral("87416418657359cb625c412a48b6e1d6d41c29bd")},
        {QStringLiteral("audio"), QStringLiteral("FFmpeg-9.0.1,16kHz,mono,PCM16,window=30s,hop=25s,RMS>0.003,new-tail>=2s")},
        {QStringLiteral("video"), QStringLiteral("FFmpeg-9.0.1,step=max(3s,ceil(duration/1200)),longest=1024,aHash=8x8,distance>4,image-budget=70")}
    };
}

inline QString pipelineContractFingerprint(const QString &prefix, const QJsonObject &contract)
{
    return prefix + QLatin1Char(':')
        + QString::fromLatin1(QCryptographicHash::hash(
            QJsonDocument(contract).toJson(QJsonDocument::Compact), QCryptographicHash::Sha256).toHex());
}

inline QString embeddingPipelineFingerprint()
{
    return pipelineContractFingerprint(QStringLiteral("qcopilots-search-embedding-v1"), embeddingPipelineContract());
}

inline QString extractionPipelineFingerprint()
{
    return pipelineContractFingerprint(QStringLiteral("qcopilots-search-extraction-v3"), extractionPipelineContract());
}
} // namespace qcopilots_search
