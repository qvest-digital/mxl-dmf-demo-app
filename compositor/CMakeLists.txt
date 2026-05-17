cmake_minimum_required(VERSION 3.20)
project(mxl-multi-compositor CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED IMPORTED_TARGET gstreamer-1.0 gstreamer-app-1.0)

add_executable(mxl-multi-compositor src/main.cpp)
target_compile_options(mxl-multi-compositor PRIVATE -Wall -Wextra -Wpedantic)

# mxl headers vendored under vendor/ — runtime libmxl.so is provided by the
# mxl-base image. No pkg-config entry available because mxl-base does not
# ship a .pc file; we just link against libmxl directly.
target_include_directories(mxl-multi-compositor PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/vendor)

target_link_libraries(mxl-multi-compositor PRIVATE PkgConfig::GST mxl pthread)

install(TARGETS mxl-multi-compositor RUNTIME DESTINATION bin)
