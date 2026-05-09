cmake_minimum_required(VERSION 3.16)
project(pix-tool-set-pdb-resolver LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(pdb-resolver
    src/main.cpp
    src/pdb_resolver.cpp
    src/json_writer.cpp
    src/dxbc_parser.cpp
)

target_include_directories(pdb-resolver PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(pdb-resolver PRIVATE ole32 oleaut32)

set_target_properties(pdb-resolver PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/bin"
    RUNTIME_OUTPUT_DIRECTORY_DEBUG "${CMAKE_CURRENT_SOURCE_DIR}/bin"
    RUNTIME_OUTPUT_DIRECTORY_RELEASE "${CMAKE_CURRENT_SOURCE_DIR}/bin"
)
