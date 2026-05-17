cmake_minimum_required(VERSION 3.20)
project(mxl-multi-compositor CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(PkgConfig REQUIRED)
pkg_check_modules(GST REQUIRED IMPORTED_TARGET gstreamer-1.0 gstreamer-app-1.0)

# libmxl ships its own pkg-config via /usr/lib/pkgconfig/mxl.pc (provided by
# the mxl-base image's libmxl install). Fallback to manual link if pkg-config
# entry not found, so the build still works on dev hosts without pkg-config
# integration.
pkg_check_modules(MXL IMPORTED_TARGET mxl)

add_executable(mxl-multi-compositor src/main.cpp)
target_compile_options(mxl-multi-compositor PRIVATE -Wall -Wextra -Wpedantic)

if(MXL_FOUND)
  target_link_libraries(mxl-multi-compositor PRIVATE PkgConfig::MXL PkgConfig::GST)
else()
  target_link_libraries(mxl-multi-compositor PRIVATE PkgConfig::GST mxl)
endif()
target_link_libraries(mxl-multi-compositor PRIVATE pthread)

install(TARGETS mxl-multi-compositor RUNTIME DESTINATION bin)
