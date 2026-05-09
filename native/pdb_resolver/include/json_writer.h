#pragma once

#include <string>
#include <sstream>
#include <vector>

// Minimal JSON writer — no dependencies
class JsonWriter {
public:
    JsonWriter() : m_indent(0), m_needComma(false) {}

    void beginObject() {
        maybeComma();
        m_ss << "{\n";
        m_indent++;
        m_needComma = false;
    }
    void endObject() {
        m_ss << "\n";
        m_indent--;
        writeIndent();
        m_ss << "}";
        m_needComma = true;
    }

    void beginArray() {
        maybeComma();
        m_ss << "[\n";
        m_indent++;
        m_needComma = false;
    }
    void endArray() {
        m_ss << "\n";
        m_indent--;
        writeIndent();
        m_ss << "]";
        m_needComma = true;
    }

    void key(const std::string& k) {
        maybeComma();
        m_ss << "\"" << escapeJson(k) << "\": ";
        m_needComma = false;
    }

    void valueString(const std::string& v) {
        maybeComma();
        m_ss << "\"" << escapeJson(v) << "\"";
        m_needComma = true;
    }

    void valueInt(int64_t v) {
        maybeComma();
        m_ss << v;
        m_needComma = true;
    }

    void valueBool(bool v) {
        maybeComma();
        m_ss << (v ? "true" : "false");
        m_needComma = true;
    }

    void valueNull() {
        maybeComma();
        m_ss << "null";
        m_needComma = true;
    }

    // Convenience: write key-value pairs
    void kv(const std::string& k, const std::string& v) {
        key(k); valueString(v);
    }
    void kv(const std::string& k, int64_t v) {
        key(k); valueInt(v);
    }
    void kv(const std::string& k, bool v) {
        key(k); valueBool(v);
    }
    void kvNull(const std::string& k) {
        key(k); valueNull();
    }

    std::string str() const { return m_ss.str(); }

private:
    void writeIndent() {
        for (int i = 0; i < m_indent; i++) m_ss << "  ";
    }
    void maybeComma() {
        if (m_needComma) {
            m_ss << ",\n";
        }
        writeIndent();
    }

    static std::string escapeJson(const std::string& s) {
        std::string out;
        out.reserve(s.size() + 16);
        for (char c : s) {
            switch (c) {
            case '\"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", (unsigned char)c);
                    out += buf;
                } else {
                    out += c;
                }
                break;
            }
        }
        return out;
    }

    std::stringstream m_ss;
    int m_indent;
    bool m_needComma;
};
