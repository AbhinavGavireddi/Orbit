import Foundation
import Security

enum SecretStore {
    private static let service = "dev.orbit.assistant"

    static var openai: String { load("openai") ?? "" }
    static var jev: String { load("jev") ?? "" }
    static var deviceToken: String { load("device") ?? "" }
    static var serviceToken: String { load("service") ?? "" }
    static var hasKeys: Bool {
        openai.count >= 8 && deviceToken.count >= 32 && serviceToken.count >= 32 && deviceToken != serviceToken
    }

    static func saveKeys(openai: String, jev: String) {
        save(openai.trimmingCharacters(in: .whitespacesAndNewlines), account: "openai")
        save(jev.trimmingCharacters(in: .whitespacesAndNewlines), account: "jev")
        ensureTokens()
    }

    static func ensureTokens() {
        var device = deviceToken
        var serviceValue = serviceToken
        if device.count < 32 { device = token(); save(device, account: "device") }
        if serviceValue.count < 32 || serviceValue == device {
            repeat { serviceValue = token() } while serviceValue == device
            save(serviceValue, account: "service")
        }
    }

    private static func token() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "A")
            .replacingOccurrences(of: "/", with: "B")
            .replacingOccurrences(of: "=", with: "")
    }

    private static func save(_ value: String, account: String) {
        let query = base(account)
        SecItemDelete(query as CFDictionary)
        var add = query
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(add as CFDictionary, nil)
    }

    private static func load(_ account: String) -> String? {
        var query = base(account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func base(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }
}
