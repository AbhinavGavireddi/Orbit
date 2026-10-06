// swift-tools-version: 6.0
import PackageDescription
let package = Package(
    name: "OrbitMac",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "Orbit", targets: ["OrbitApp"])],
    targets: [
        .target(name: "OrbitCore"),
        .executableTarget(name: "OrbitApp", dependencies: ["OrbitCore"]),
        .testTarget(
            name: "OrbitCoreTests",
            dependencies: ["OrbitCore"],
            resources: [.copy("Fixtures")]
        )
    ],
    swiftLanguageModes: [.v5]
)
